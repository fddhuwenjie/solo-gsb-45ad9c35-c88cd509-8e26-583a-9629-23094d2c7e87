"""Extract a runnable relational-closure subset from an existing fixture.

``POST /fixtures/slice`` picks the root rows that match ``root.column == value``,
then expands along every foreign key edge until the closure stabilises:

* **parents are mandatory** — every row referenced by an included row's foreign
  key must be included, transitively, so the slice stays loadable as-is;
* **children are optional** — rows referencing an included row are admitted
  while the per-table limits allow, in a seed-deterministic order;
* **limits never break the closure** — if the mandatory rows (roots + their
  transitive parents) exceed a table's limit, or an admitted child would force
  parents past a limit, the whole request fails with 422 instead of emitting a
  half-closed slice;
* tables the closure never touches keep an empty array; row contents, field
  names and constraints are copied verbatim.

All validation (schema, root condition, fixture shape, raw unique/FK
integrity) runs before any data is emitted, so a 422 never carries a partial
slice.
"""

from types import SimpleNamespace
from typing import Any, Optional, Union

from .anonymizer import (
    _conformance_error,
    _effective_fields,
    _validate_fixture_shape,
    _verify_raw_integrity,
)
from .errors import FixtureValidationError
from .generator import child_rng
from .models import FixtureResponse, ForeignKey, SliceRoot, TableSpec
from .planner import build_plan


def _err(
    kind: str,
    msg: str,
    loc: Optional[list[Union[str, int]]] = None,
) -> dict[str, Any]:
    """Build an error envelope matching FastAPI's standard 422 shape."""
    return {"loc": ["body"] + (loc or []), "type": kind, "msg": msg}


def _type_matches(value: Any, type_name: str) -> bool:
    """Strict JSON-level type check for the root filter value."""
    if value is None:
        return True
    if type_name == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "boolean":
        return isinstance(value, bool)
    return isinstance(value, str)


class _Closure:
    """Incremental relational-closure builder over one fixture.

    Tracks the selected row indices per table and answers, for any candidate
    row, which *additional* rows its foreign keys would force into the slice.
    """

    def __init__(
        self,
        tables: list[TableSpec],
        by_name: dict[str, TableSpec],
        fixture_tables: dict[str, list[dict[str, Any]]],
    ) -> None:
        self._by_name = by_name
        self._fixture_tables = fixture_tables
        # table -> selected row indices
        self.selected: dict[str, set[int]] = {t.name: set() for t in tables}
        # parent table -> [(child table, fk column, parent column)]
        self._in_fks: dict[str, list[tuple[str, str, str]]] = {
            t.name: [] for t in tables
        }
        for t in tables:
            for fk in t.foreign_keys:
                self._in_fks[fk.references_table].append(
                    (t.name, fk.column, fk.references_column)
                )
        # (table, column) -> {column value: row index}; FK targets are unique
        # (verified on the raw fixture), so one value maps to one row.
        self._target_index: dict[tuple[str, str], dict[Any, int]] = {}

    # ------------------------------------------------------------- parents

    def _parent_of(self, row: dict[str, Any], fk: ForeignKey) -> Optional[int]:
        value = row.get(fk.column)
        if value is None:
            return None
        key = (fk.references_table, fk.references_column)
        index = self._target_index.get(key)
        if index is None:
            index = {}
            for i, parent_row in enumerate(self._fixture_tables[fk.references_table]):
                index[parent_row[fk.references_column]] = i
            self._target_index[key] = index
        return index.get(value)

    def collect_parents(self, table: str, row_index: int) -> dict[str, set[int]]:
        """Rows that including ``(table, row_index)`` forces into the slice:
        the row's transitive parents that are not selected yet."""
        forced: dict[str, set[int]] = {}
        stack = [(table, row_index)]
        while stack:
            t_name, r_i = stack.pop()
            row = self._fixture_tables[t_name][r_i]
            for fk in self._by_name[t_name].foreign_keys:
                p_i = self._parent_of(row, fk)
                if p_i is None:
                    continue
                p_name = fk.references_table
                if p_i in self.selected[p_name] or p_i in forced.get(p_name, ()):
                    continue
                forced.setdefault(p_name, set()).add(p_i)
                stack.append((p_name, p_i))
        return forced

    # ------------------------------------------------------------- children

    def candidate_children(self) -> dict[str, set[int]]:
        """Unselected rows that reference at least one selected row."""
        out: dict[str, set[int]] = {name: set() for name in self.selected}
        for parent_name, in_fks in self._in_fks.items():
            if not self.selected[parent_name]:
                continue
            for child_name, fk_column, parent_column in in_fks:
                wanted = {
                    self._fixture_tables[parent_name][i][parent_column]
                    for i in self.selected[parent_name]
                }
                for r_i, row in enumerate(self._fixture_tables[child_name]):
                    if r_i in self.selected[child_name]:
                        continue
                    value = row.get(fk_column)
                    if value is not None and value in wanted:
                        out[child_name].add(r_i)
        return out

    # -------------------------------------------------------------- commit

    def admit(self, table: str, row_index: int, forced: dict[str, set[int]]) -> None:
        self.selected[table].add(row_index)
        for t_name, indices in forced.items():
            self.selected[t_name] |= indices


def slice_fixture(
    fixture: FixtureResponse,
    tables: list[TableSpec],
    root: SliceRoot,
    limits: dict[str, int],
    seed: Union[int, str],
) -> FixtureResponse:
    by_name = {t.name: t for t in tables}
    # build_plan rejects duplicate table names, unknown FK targets and cycles.
    plan = build_plan(SimpleNamespace(tables=tables, seed=seed))

    # ------------------------------------------------- validate the request
    errors: list[dict[str, Any]] = []
    for table in limits:
        if table not in by_name:
            errors.append(_err(
                "unknown_table",
                f"行数上限引用的表 {table!r} 不存在",
                ["limits", table],
            ))
    if root.table not in by_name:
        errors.append(_err(
            "unknown_table",
            f"根表筛选条件引用的表 {root.table!r} 不存在",
            ["root", "table"],
        ))
        root_spec = None
    else:
        effective_root = _effective_fields(by_name[root.table], by_name)
        if root.column not in effective_root:
            errors.append(_err(
                "unknown_column",
                f"根表筛选条件引用的列 {root.table}.{root.column} 不存在",
                ["root", "column"],
            ))
            root_spec = None
        else:
            root_spec = effective_root[root.column]
            if root.value is not None:
                problem = _conformance_error(
                    root.value, root_spec, ["root", "value"]
                )
                if problem is not None:
                    problem["type"] = "invalid_filter_value"
                    errors.append(problem)
    if errors:
        raise FixtureValidationError(errors)

    # ------------------------------------- fixture structure and integrity
    fixture_tables = fixture.tables
    _validate_fixture_shape(fixture, by_name, plan.order, check_row_counts=False)
    _verify_raw_integrity(tables, by_name, plan, fixture_tables)

    closure = _Closure(tables, by_name, fixture_tables)

    # ------------------------------------------------------- pick the roots
    assert root_spec is not None  # guaranteed by the validation above
    root_type = root_spec.type
    roots = [
        i
        for i, row in enumerate(fixture_tables[root.table])
        if _type_matches(row[root.column], root_type) and row[root.column] == root.value
    ]

    # --------------------------- mandatory closure: roots + their parents
    mandatory: dict[str, set[int]] = {t.name: set() for t in tables}
    mandatory[root.table] = set(roots)
    for r_i in roots:
        for t_name, indices in closure.collect_parents(root.table, r_i).items():
            mandatory[t_name] |= indices
    for t_name, indices in mandatory.items():
        cap = limits.get(t_name)
        if cap is not None and len(indices) > cap:
            raise FixtureValidationError([_err(
                "limit_blocks_closure",
                f"表 {t_name!r} 的行数上限 {cap} 小于闭包必需行数 "
                f"{len(indices)}（根行与其全部父行不可截断）",
                ["limits", t_name],
            )])
    for t_name, indices in mandatory.items():
        closure.selected[t_name] |= indices

    # ----------------- optional children, truncated deterministically
    # Rows referencing selected rows are optional: admit them while the per-
    # table limits allow, scanning each table's candidates in a seed-shuffled
    # order so the truncation is stable for a given seed. A child whose own
    # parents would overflow a limit blocks the whole slice with 422.
    shuffled: dict[str, list[int]] = {}
    for t in tables:
        rows_idx = list(range(len(fixture_tables[t.name])))
        child_rng(seed, "slice", t.name).shuffle(rows_idx)
        shuffled[t.name] = rows_idx

    while True:
        candidates = closure.candidate_children()
        if not any(candidates.values()):
            break
        admitted_any = False
        for t_name in plan.order:
            if not candidates[t_name]:
                continue
            for r_i in shuffled[t_name]:
                if r_i not in candidates[t_name] or r_i in closure.selected[t_name]:
                    continue
                cap = limits.get(t_name)
                if cap is not None and len(closure.selected[t_name]) + 1 > cap:
                    continue  # limit reached: truncate this optional child
                forced = closure.collect_parents(t_name, r_i)
                for f_name, indices in forced.items():
                    f_cap = limits.get(f_name)
                    if f_cap is None:
                        continue
                    # The candidate itself also occupies a slot when its own
                    # table is the forced parent table (self-reference).
                    extra = len(indices) + (1 if f_name == t_name else 0)
                    if len(closure.selected[f_name]) + extra > f_cap:
                        raise FixtureValidationError([_err(
                            "limit_blocks_closure",
                            f"纳入 {t_name} 第 {r_i} 行需要同时纳入其父行，"
                            f"但表 {f_name!r} 的行数上限 {f_cap} 无法容纳",
                            ["limits", f_name],
                        )])
                closure.admit(t_name, r_i, forced)
                admitted_any = True
        if not admitted_any:
            break

    # ------------------------------------- defensive: the slice must close
    _verify_slice_foreign_keys(tables, closure.selected, fixture_tables)

    # Rows keep their original fixture order; untouched tables stay empty.
    out = {
        t.name: [fixture_tables[t.name][i] for i in sorted(closure.selected[t.name])]
        for t in tables
    }
    return FixtureResponse(
        seed=seed,
        row_counts={name: len(rows) for name, rows in out.items()},
        tables=out,
    )


def _verify_slice_foreign_keys(
    tables: list[TableSpec],
    selected: dict[str, set[int]],
    fixture_tables: dict[str, list[dict[str, Any]]],
) -> None:
    """Every non-null FK of every selected row must point at a selected row."""
    errors: list[dict[str, Any]] = []
    for table in tables:
        for fk in table.foreign_keys:
            target_values = {
                fixture_tables[fk.references_table][i][fk.references_column]
                for i in selected[fk.references_table]
            }
            for r_i in sorted(selected[table.name]):
                value = fixture_tables[table.name][r_i].get(fk.column)
                if value is None:
                    continue
                if value not in target_values:
                    errors.append(_err(
                        "slice_not_closed",
                        f"裁剪结果不完整：{table.name} 第 {r_i} 行的外键 "
                        f"{fk.column}={value!r} 未包含其引用的 "
                        f"{fk.references_table}.{fk.references_column} 记录",
                        ["limits"],
                    ))
    if errors:
        raise FixtureValidationError(errors)
