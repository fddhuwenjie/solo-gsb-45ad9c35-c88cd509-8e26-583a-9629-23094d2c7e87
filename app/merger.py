"""Merge two or more independently generated fixtures into one importable fixture.

``POST /fixtures/merge`` receives several fixtures that share one table
schema plus a merge seed, and combines them into a single fixture that still
satisfies every unique and foreign-key constraint:

* every fixture is validated first — table set, ``row_counts`` consistency,
  per-column type conformance, single/composite uniqueness and foreign-key
  integrity — and any problem fails the whole request with 422;
* rows that share a non-null unique key **and** are entirely identical
  (extra fields included) are the same logical record: only the first copy
  is kept (SQL semantics: keys containing NULL never collide);
* rows whose unique keys collide but differ elsewhere keep the first
  occurrence; later rows receive freshly generated, format-legal replacement
  values drawn from the merge seed (respecting type, range, length, prefix
  and choices; FK columns pick another existing parent value);
* every rewrite cascades along the FK dependency graph — across tables,
  through self-references and over composite constraints — so references
  from the same source fixture keep pointing at the rewritten row, while
  other fixtures' references stay untouched;
* non-conflicting values and fields outside the schema are copied verbatim;
  row order is stable (fixture order, then original row order) and tables
  follow the schema declaration order.

If the constraints cannot be satisfied (e.g. a unique boolean column would
need a third distinct value), the whole request fails with 422 and no
partial data is returned.
"""

from itertools import product
from types import SimpleNamespace
from typing import Any, Optional, Union

from .anonymizer import (
    _effective_fields,
    _validate_fixture_shape,
    _verify_raw_integrity,
)
from .errors import FixtureValidationError
from .generator import (
    FIRST_NAMES,
    LAST_NAMES,
    STRING_ALPHABET,
    _domain_size,
    _random_value,
    child_rng,
)
from .models import FieldSpec, FixtureResponse, ForeignKey, TableSpec
from .planner import Plan, build_plan

_TRY_CANDIDATES = 1000  # rejection-sampling draws per replacement value
_ENUMERATE_LIMIT = 100_000  # largest value domain enumerated exhaustively
_DEFAULT_INT_MIN, _DEFAULT_INT_MAX = 1, 1_000_000  # mirror generator defaults
_DEFAULT_STR_MIN, _DEFAULT_STR_MAX = 8, 16
_MISSING = object()


def _err(
    kind: str,
    msg: str,
    loc: Optional[list[Union[str, int]]] = None,
) -> dict[str, Any]:
    """Build an error envelope matching FastAPI's standard 422 shape."""
    return {"loc": ["body"] + (loc or []), "type": kind, "msg": msg}


# ------------------------------------------------------------- per-fixture
# validation


def _validate_one_fixture(
    fx: FixtureResponse,
    f_i: int,
    tables: list[TableSpec],
    by_name: dict[str, TableSpec],
    plan: Plan,
    all_cols: set[tuple[str, str]],
) -> list[dict[str, Any]]:
    """Validate one fixture against the shared schema.

    Error locations are rooted at ``fixtures[f_i]`` so the client can tell
    which input fixture failed.
    """
    prefix: list[Union[str, int]] = ["fixtures", f_i]

    def refix(details: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for d in details:
            loc = list(d["loc"])
            if len(loc) >= 2 and loc[1] == "fixture":
                loc = ["body"] + prefix + loc[2:]
            out.append({**d, "loc": loc})
        return out

    try:
        _validate_fixture_shape(
            fx,
            by_name,
            plan.order,
            check_row_counts=False,
            conformance=all_cols,
            conformance_loc_builder=lambda name, r_i, col: [
                "fixture", "tables", name, r_i, col
            ],
        )
    except FixtureValidationError as exc:
        # Shape is broken; deeper integrity checks would not be meaningful.
        return refix(exc.details)

    errors: list[dict[str, Any]] = []
    for name in plan.order:
        declared = fx.row_counts.get(name)
        actual = len(fx.tables[name])
        if declared != actual:
            errors.append(_err(
                "fixture_shape_mismatch",
                f"fixtures[{f_i}] 的 row_counts[{name!r}]={declared} "
                f"与实际行数 {actual} 不一致",
                prefix + ["row_counts", name],
            ))
    try:
        _verify_raw_integrity(tables, by_name, plan, fx.tables)
    except FixtureValidationError as exc:
        errors.extend(refix(exc.details))
    return errors


# ------------------------------------------------------------- value domains


def _freeze(value: Any) -> Any:
    """Hashable canonical form of a JSON value (dicts become sorted tuples)."""
    if isinstance(value, dict):
        return tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def _enumerate_domain(spec: FieldSpec) -> list[Any]:
    """Every legal value of a small, finite domain (exhaustive fallback)."""
    t = spec.type
    if t == "boolean":
        return [True, False]
    if t == "enum":
        return list(spec.choices or [])
    if t == "name":
        return [f"{f} {l}" for f in FIRST_NAMES for l in LAST_NAMES]
    if t == "int":
        lo = int(spec.min_value) if spec.min_value is not None else _DEFAULT_INT_MIN
        hi = int(spec.max_value) if spec.max_value is not None else _DEFAULT_INT_MAX
        if hi - lo + 1 <= _ENUMERATE_LIMIT:
            return list(range(lo, hi + 1))
        return []
    if t == "string":
        lo = spec.min_length if spec.min_length is not None else _DEFAULT_STR_MIN
        hi = spec.max_length if spec.max_length is not None else _DEFAULT_STR_MAX
        base = len(STRING_ALPHABET)
        if sum(base ** n for n in range(lo, hi + 1)) > _ENUMERATE_LIMIT:
            return []
        out = []
        for n in range(lo, hi + 1):
            for combo in product(STRING_ALPHABET, repeat=n):
                body = "".join(combo)
                out.append(f"{spec.prefix}{body}" if spec.prefix else body)
        return out
    return []  # float, email, uuid, date, datetime: effectively unbounded


class _Counts:
    """Value multiplicities plus the number of rows overflowing the first copy."""

    __slots__ = ("counts", "excess")

    def __init__(self) -> None:
        self.counts: dict[Any, int] = {}
        self.excess = 0

    def add(self, key: Any, delta: int) -> None:
        old = self.counts.get(key, 0)
        new = old + delta
        if delta > 0 and new >= 2:
            self.excess += 1
        elif delta < 0 and old >= 2:
            self.excess -= 1
        if new:
            self.counts[key] = new
        else:
            self.counts.pop(key, None)


# ------------------------------------------------------------- table merging


def _merge_table(
    name: str,
    t_idx: int,
    table: TableSpec,
    fixtures: list[FixtureResponse],
    eff: dict[str, FieldSpec],
    fks: dict[str, ForeignKey],
    referenced_here: set[str],
    self_by_target: dict[str, list[str]],
    seed: Union[int, str],
    cascades: dict[tuple[str, str], dict[tuple[int, Any], Any]],
    pools: dict[str, dict[str, list[Any]]],
) -> list[list[Any]]:
    """Merge one table's rows across all fixtures.

    Returns ``[[fixture_index, row], ...]`` in stable merge order; rewrites
    of referenced columns are recorded into ``cascades`` so child tables can
    follow them.
    """
    rows: list[list[Any]] = []
    for f_i, fx in enumerate(fixtures):
        for row in fx.tables[name]:
            rows.append([f_i, dict(row)])

    # Rewrites from already-merged parent tables: a reference from fixture
    # f to a rewritten parent key follows that key's new value.
    for col, fk in fks.items():
        if fk.references_table == name:
            continue  # self-references are handled together with their target
        mapping = cascades.get((fk.references_table, fk.references_column))
        if not mapping:
            continue
        for f_i, row in rows:
            value = row.get(col)
            if value is None:
                continue
            new_value = mapping.get((f_i, value), _MISSING)
            if new_value is not _MISSING:
                row[col] = new_value

    unique_cols = [c for c, s in eff.items() if s.unique]
    composite = [tuple(uc) for uc in table.unique_constraints]

    # Fully identical rows sharing a non-null unique key are the same
    # logical record: keep the first copy only. SQL NULL semantics: keys
    # containing NULL never collide, so such duplicates all stay.
    if unique_cols or composite:

        def has_key(r: dict[str, Any]) -> bool:
            if any(r.get(c) is not None for c in unique_cols):
                return True
            return any(
                None not in tuple(r.get(c) for c in uc) for uc in composite
            )

        seen: set = set()
        deduped: list[list[Any]] = []
        for f_i, row in rows:
            frozen = _freeze(row)
            if frozen in seen and has_key(row):
                continue
            seen.add(frozen)
            deduped.append([f_i, row])
        rows = deduped

    # --- uniqueness state --------------------------------------------------
    col_counts = {c: _Counts() for c in unique_cols}
    tup_counts = [_Counts() for _ in composite]
    for _, row in rows:
        for c in unique_cols:
            v = row.get(c)
            if v is not None:
                col_counts[c].add(v, +1)
        for i, uc in enumerate(composite):
            key = tuple(row.get(c) for c in uc)
            if None not in key:
                tup_counts[i].add(key, +1)

    def pool_values(fk: ForeignKey) -> list[Any]:
        if fk.references_table == name:
            return [
                r.get(fk.references_column) for _, r in rows
                if r.get(fk.references_column) is not None
            ]
        return pools.get(fk.references_table, {}).get(fk.references_column, [])

    def domain_of(col: str) -> Optional[int]:
        fk = fks.get(col)
        if fk is not None:
            return len(set(pool_values(fk)))
        return _domain_size(eff[col])

    # --- feasibility: the merged value space must fit every unique key -----
    for c in unique_cols:
        needed = sum(1 for _, r in rows if r.get(c) is not None)
        size = domain_of(c)
        if size is not None and size < needed:
            raise FixtureValidationError([_err(
                "unique_space_exhausted",
                f"合并后唯一列 {name}.{c} 需要 {needed} 个不同值，"
                f"但取值空间仅 {size} 种",
                ["tables", str(t_idx), "fields", c],
            )])
    for i, uc in enumerate(composite):
        needed = sum(
            1 for _, r in rows if all(r.get(c) is not None for c in uc)
        )
        sizes = [domain_of(c) for c in uc]
        if all(s is not None for s in sizes):
            space = 1
            for s in sizes:
                space *= s  # type: ignore[operator]
            if space < needed:
                raise FixtureValidationError([_err(
                    "unique_space_exhausted",
                    f"合并后复合唯一约束 {list(uc)} 需要 {needed} 种组合，"
                    f"但取值空间仅 {space} 种",
                    ["tables", str(t_idx), "unique_constraints", str(i)],
                )])

    # --- conflict resolution ------------------------------------------------
    rngs: dict[str, Any] = {}

    def col_rng(col: str):
        rng = rngs.get(col)
        if rng is None:
            rng = rngs[col] = child_rng(seed, "merge", name, col)
        return rng

    def assignment_ok(row: dict[str, Any], assigns: dict[str, Any]) -> bool:
        """A tentative assignment must not collide in any unique structure."""
        for c, v in assigns.items():
            if c in col_counts and col_counts[c].counts.get(v, 0):
                return False
        for i, uc in enumerate(composite):
            if not any(c in assigns for c in uc):
                continue
            key = tuple(assigns.get(c, row.get(c)) for c in uc)
            if None in key:
                continue
            if tup_counts[i].counts.get(key, 0):
                return False
        return True

    def pick_value(idx: int, col: str) -> Any:
        """One fresh, format-legal value for ``col`` (FKs draw from the pool)."""
        row = rows[idx][1]
        fk = fks.get(col)
        rng = col_rng(col)
        if fk is not None:
            pool = sorted(set(pool_values(fk)), key=repr)
            rng.shuffle(pool)
            for cand in pool:
                if assignment_ok(row, {col: cand}):
                    return cand
            return None
        spec = eff[col]
        for _ in range(_TRY_CANDIDATES):
            cand = _random_value(rng, spec)
            if assignment_ok(row, {col: cand}):
                return cand
        for cand in _enumerate_domain(spec):
            if assignment_ok(row, {col: cand}):
                return cand
        return None

    def pick_tuple(idx: int, uc: tuple) -> Optional[dict[str, Any]]:
        """Rewrite a whole composite key at once (tightly coupled domains)."""
        row = rows[idx][1]
        fk_pools: dict[str, list[Any]] = {}
        for c in uc:
            fk = fks.get(c)
            if fk is not None:
                pool = sorted(set(pool_values(fk)), key=repr)
                if not pool:
                    return None
                fk_pools[c] = pool
        for _ in range(_TRY_CANDIDATES):
            assigns = {}
            for c in uc:
                if c in fk_pools:
                    pool = fk_pools[c]
                    assigns[c] = pool[col_rng(c).randrange(len(pool))]
                else:
                    assigns[c] = _random_value(col_rng(c), eff[c])
            if assignment_ok(row, assigns):
                return assigns
        domains = []
        for c in uc:
            if c in fk_pools:
                domains.append(fk_pools[c])
            else:
                domain = _enumerate_domain(eff[c])
                if not domain:
                    return None
                domains.append(domain)
        space = 1
        for domain in domains:
            space *= len(domain)
        if space <= _ENUMERATE_LIMIT:
            for combo in product(*domains):
                assigns = dict(zip(uc, combo))
                if assignment_ok(row, assigns):
                    return assigns
        return None

    def set_value(idx: int, col: str, new_value: Any) -> None:
        f_i, row = rows[idx]
        old = row[col]
        old_keys = {
            i: tuple(row.get(c) for c in uc)
            for i, uc in enumerate(composite)
            if col in uc
        }
        row[col] = new_value
        if col in col_counts:
            col_counts[col].add(old, -1)
            col_counts[col].add(new_value, +1)
        for i, old_key in old_keys.items():
            if None not in old_key:
                tup_counts[i].add(old_key, -1)
            new_key = tuple(row.get(c) for c in composite[i])
            if None not in new_key:
                tup_counts[i].add(new_key, +1)
        if col in referenced_here:
            cascades.setdefault((name, col), {})[(f_i, old)] = new_value
        # Self-references from the same fixture follow the rewritten key.
        for sc in self_by_target.get(col, ()):
            for j, (f_j, other) in enumerate(rows):
                if f_j == f_i and other.get(sc) == old:
                    set_value(j, sc, new_value)

    def set_values(idx: int, assigns: dict[str, Any]) -> None:
        for c, v in assigns.items():
            set_value(idx, c, v)

    max_rounds = max(
        1000, 10 * (len(rows) + 1) * (len(unique_cols) + len(composite) + 1)
    )
    for _ in range(max_rounds):
        single = next((c for c in unique_cols if col_counts[c].excess), None)
        comp_i = next(
            (i for i in range(len(composite)) if tup_counts[i].excess), None
        )
        if single is None and comp_i is None:
            break
        if single is not None:
            # Keep the first occurrence; rewrite a later duplicate.
            seen_keys: set = set()
            idx = -1
            for j, (_, r) in enumerate(rows):
                v = r.get(single)
                if v is None:
                    continue
                if v in seen_keys:
                    idx = j
                    break
                seen_keys.add(v)
            new_value = pick_value(idx, single)
            if new_value is None:
                raise FixtureValidationError([_err(
                    "unique_space_exhausted",
                    f"无法为合并冲突的列 {name}.{single} 生成格式合法的"
                    "新唯一值（取值空间已耗尽）",
                    ["tables", str(t_idx), "fields", single],
                )])
            set_value(idx, single, new_value)
            continue
        uc = composite[comp_i]
        seen_keys = set()
        idx = -1
        for j, (_, r) in enumerate(rows):
            key = tuple(r.get(c) for c in uc)
            if None in key:
                continue
            if key in seen_keys:
                idx = j
                break
            seen_keys.add(key)
        # Minimal rewrite first: change one column of the key (plain columns
        # before pool-bound FK columns); fall back to the whole key when
        # tightly coupled domains leave no single-column escape.
        assigns = None
        for c in [c for c in uc if c not in fks] + [c for c in uc if c in fks]:
            cand = pick_value(idx, c)
            if cand is not None:
                assigns = {c: cand}
                break
        if assigns is None:
            assigns = pick_tuple(idx, uc)
        if assigns is None:
            raise FixtureValidationError([_err(
                "unique_space_exhausted",
                f"无法为合并冲突的复合唯一约束 {list(uc)} 生成新的唯一组合"
                "（取值空间已耗尽）",
                ["tables", str(t_idx), "unique_constraints", str(comp_i)],
            )])
        set_values(idx, assigns)
    else:
        raise FixtureValidationError([_err(
            "unique_space_exhausted",
            f"表 {name!r} 的合并冲突在 {max_rounds} 轮内未能全部解决",
            ["tables", str(t_idx)],
        )])

    return rows


# ------------------------------------------------------------- main entry


def merge(
    fixtures: list[FixtureResponse],
    tables: list[TableSpec],
    seed: Union[int, str],
) -> FixtureResponse:
    by_name = {t.name: t for t in tables}
    # build_plan rejects duplicate table names, unknown FK targets and cycles.
    plan = build_plan(SimpleNamespace(tables=tables, seed=seed))

    effective = {t.name: _effective_fields(t, by_name) for t in tables}
    fks_by_col = {
        t.name: {fk.column: fk for fk in t.foreign_keys} for t in tables
    }

    # ---------------- validate every fixture before merging anything
    all_cols = {(name, col) for name, eff in effective.items() for col in eff}
    errors: list[dict[str, Any]] = []
    for f_i, fx in enumerate(fixtures):
        errors.extend(
            _validate_one_fixture(fx, f_i, tables, by_name, plan, all_cols)
        )
    if errors:
        raise FixtureValidationError(errors)

    # ---------------- merge tables in dependency order
    referenced: dict[str, set[str]] = {t.name: set() for t in tables}
    self_by_target: dict[str, dict[str, list[str]]] = {
        t.name: {} for t in tables
    }
    for t in tables:
        for fk in t.foreign_keys:
            referenced[fk.references_table].add(fk.references_column)
            if fk.references_table == t.name:
                self_by_target[t.name].setdefault(
                    fk.references_column, []
                ).append(fk.column)

    cascades: dict[tuple[str, str], dict[tuple[int, Any], Any]] = {}
    pools: dict[str, dict[str, list[Any]]] = {}
    merged: dict[str, list[dict[str, Any]]] = {}
    for name in plan.order:
        t_idx = next(i for i, t in enumerate(tables) if t.name == name)
        rows = _merge_table(
            name, t_idx, by_name[name], fixtures, effective[name],
            fks_by_col[name], referenced[name], self_by_target[name],
            seed, cascades, pools,
        )
        # Publish this table's unique columns for downstream FK pools.
        pools[name] = {
            c: [r.get(c) for _, r in rows if r.get(c) is not None]
            for c, s in effective[name].items()
            if s.unique
        }
        merged[name] = [r for _, r in rows]

    # Defensive: the merged fixture must satisfy every constraint.
    try:
        _verify_raw_integrity(tables, by_name, plan, merged)
    except FixtureValidationError:
        raise FixtureValidationError([_err(
            "merge_conflict_unresolvable",
            "合并结果未能满足全部唯一/外键约束",
        )])

    return FixtureResponse(
        seed=seed,
        row_counts={t.name: len(merged[t.name]) for t in tables},
        tables={t.name: merged[t.name] for t in tables},
    )
