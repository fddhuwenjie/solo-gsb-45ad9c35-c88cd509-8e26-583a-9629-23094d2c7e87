"""Semantic validation and dependency planning.

Performs cross-table checks that cannot be expressed in Pydantic validators
(unknown FK targets, dangling unique targets, dependency cycles) and produces
a deterministic topological generation order.
"""

from dataclasses import dataclass, field

from .errors import FixtureValidationError
from .models import FixtureRequest, ForeignKey, TableSpec


@dataclass
class Plan:
    """Validated generation plan."""

    order: list[str]
    # table name -> foreign keys pointing at the table itself (self reference)
    self_foreign_keys: dict[str, list[ForeignKey]] = field(default_factory=dict)


def _err(kind: str, msg: str, **loc: object) -> dict:
    location: list[str] = ["body"]
    if "table_index" in loc:
        location += ["tables", str(loc["table_index"])]
    if "section" in loc:
        location.append(str(loc["section"]))
    if "fk_index" in loc:
        location.append(str(loc["fk_index"]))
    if "uc_index" in loc:
        location += [str(loc["uc_index"])]
    if "column" in loc:
        location.append(str(loc["column"]))
    return {"loc": location, "type": kind, "msg": msg}


def build_plan(req: FixtureRequest) -> Plan:
    tables: list[TableSpec] = req.tables
    errors: list[dict] = []

    # --- duplicate table names -------------------------------------------
    seen_names: set[str] = set()
    by_name: dict[str, TableSpec] = {}
    for i, table in enumerate(tables):
        if table.name in seen_names:
            errors.append(
                _err("duplicate_table", f"表名 {table.name!r} 重复", table_index=i)
            )
        seen_names.add(table.name)
        by_name[table.name] = table

    # --- per-table checks -------------------------------------------------
    for i, table in enumerate(tables):
        cols = set(table.fields)

        for j, uc in enumerate(table.unique_constraints):
            if not uc:
                errors.append(
                    _err(
                        "empty_unique_constraint",
                        "唯一约束必须包含至少一个字段",
                        table_index=i,
                        section="unique_constraints",
                        uc_index=j,
                    )
                )
            for col in uc:
                if col not in cols:
                    errors.append(
                        _err(
                            "unknown_column",
                            f"唯一约束引用了不存在的字段 {col!r}",
                            table_index=i,
                            section="unique_constraints",
                            uc_index=j,
                            column=col,
                        )
                    )

        fk_cols = [fk.column for fk in table.foreign_keys]
        for j, fk in enumerate(table.foreign_keys):
            if fk_cols.count(fk.column) > 1:
                errors.append(
                    _err(
                        "duplicate_fk_column",
                        f"列 {fk.column!r} 上定义了多个外键",
                        table_index=i,
                        section="foreign_keys",
                        fk_index=j,
                    )
                )

            if fk.references_table == table.name:
                if fk.references_column not in cols:
                    errors.append(
                        _err(
                            "unknown_fk_target",
                            f"自引用外键的目标列 {fk.references_column!r} 不存在",
                            table_index=i,
                            section="foreign_keys",
                            fk_index=j,
                        )
                    )
                elif not table.fields[fk.references_column].unique:
                    errors.append(
                        _err(
                            "fk_target_not_unique",
                            f"自引用外键的目标列 {fk.references_column!r} "
                            "必须标记为 unique",
                            table_index=i,
                            section="foreign_keys",
                            fk_index=j,
                        )
                    )
                continue

            if fk.references_table not in by_name:
                errors.append(
                    _err(
                        "unknown_fk_table",
                        f"外键引用的目标表 {fk.references_table!r} 不存在",
                        table_index=i,
                        section="foreign_keys",
                        fk_index=j,
                    )
                )
                continue

            target = by_name[fk.references_table]
            if fk.references_column not in target.fields:
                errors.append(
                    _err(
                        "unknown_fk_column",
                        f"目标表 {fk.references_table!r} 中不存在列 "
                        f"{fk.references_column!r}",
                        table_index=i,
                        section="foreign_keys",
                        fk_index=j,
                    )
                )
            elif not target.fields[fk.references_column].unique:
                errors.append(
                    _err(
                        "fk_target_not_unique",
                        f"外键目标 {fk.references_table}.{fk.references_column} "
                        "必须标记为 unique",
                        table_index=i,
                        section="foreign_keys",
                        fk_index=j,
                    )
                )

    if errors:
        # Fail before graph construction so every reported name resolves.
        raise FixtureValidationError(errors)

    # --- cross-table dependency graph (self edges excluded) ---------------
    dependencies: dict[str, set[str]] = {t.name: set() for t in tables}
    self_fks: dict[str, list[ForeignKey]] = {t.name: [] for t in tables}
    for t in tables:
        for fk in t.foreign_keys:
            if fk.references_table == t.name:
                self_fks[t.name].append(fk)
            else:
                dependencies[t.name].add(fk.references_table)

    # --- cycle detection via Kahn's algorithm -----------------------------
    # Edge parent -> child; a sorted worklist makes the order deterministic.
    indegree = {name: len(deps) for name, deps in dependencies.items()}
    dependents: dict[str, list[str]] = {name: [] for name in dependencies}
    for child, deps in dependencies.items():
        for parent in deps:
            dependents[parent].append(child)

    ready = sorted(name for name, deg in indegree.items() if deg == 0)
    order: list[str] = []
    while ready:
        name = ready.pop(0)
        order.append(name)
        for child in sorted(dependents[name]):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort()

    if len(order) != len(dependencies):
        cyclic = sorted(name for name, deg in indegree.items() if deg > 0)
        raise FixtureValidationError(
            [
                _err(
                    "dependency_cycle",
                    "外键依赖成环，涉及表: "
                    + " -> ".join(cyclic)
                    + "（自引用外键不计入环，但目标列须为 unique）",
                )
            ]
        )

    return Plan(order=order, self_foreign_keys=self_fks)
