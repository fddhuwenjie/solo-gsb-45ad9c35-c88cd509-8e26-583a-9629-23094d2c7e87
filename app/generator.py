"""Deterministic relational fixture generation."""

import hashlib
import random
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Optional

from .errors import FixtureValidationError
from .models import FieldSpec, FixtureRequest, TableSpec
from .planner import Plan

FIRST_NAMES = [
    "Avery", "Bailey", "Carter", "Dakota", "Emerson", "Finley", "Harper", "Jordan",
    "Kendall", "Logan", "Morgan", "Parker", "Quinn", "Reese", "Riley", "Sage",
    "Taylor", "Uma", "Vega", "Wren",
]
LAST_NAMES = [
    "Adams", "Brooks", "Chen", "Diaz", "Evans", "Foster", "Garcia", "Hayes",
    "Ito", "Jensen", "Khan", "Lopez", "Miller", "Nakamura", "Owens", "Patel",
    "Quintero", "Rossi", "Singh", "Tanaka", "Ueno", "Vargas", "Walsh", "Xu",
    "Yamada", "Zhang", "Bell", "Clark", "Dunn", "Ellis",
]

_STRING_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
# Public aliases reused by the anonymizer so both modules draw from one source.
STRING_ALPHABET = _STRING_ALPHABET
_NULL_RATE = 0.10
_FLOAT_PRECISION = 2
_DEFAULT_INT_MIN, _DEFAULT_INT_MAX = 1, 1_000_000
_DEFAULT_STR_MIN, _DEFAULT_STR_MAX = 8, 16
_DATE_SPAN_DAYS = 365 * 30  # 2000-01-01 .. 2029-12-27
DATE_SPAN_START = date(2000, 1, 1)
_EMAIL_DOMAIN = "example.com"
_NAME_SPACE = len(FIRST_NAMES) * len(LAST_NAMES)
NAME_SPACE = _NAME_SPACE


def child_rng(master_seed: object, *parts: object) -> random.Random:
    """Derive an independent deterministic RNG from ``(seed, *parts)``.

    Every column/FK gets its own RNG keyed by table + column name, so the
    stream for a column is stable regardless of topological generation order
    or request table ordering.
    """
    digest = hashlib.sha256()
    digest.update(str(master_seed).encode("utf-8"))
    for part in parts:
        digest.update(b"\x00")
        digest.update(str(part).encode("utf-8"))
    return random.Random(int.from_bytes(digest.digest()[:8], "big"))


def _domain_size(spec: FieldSpec) -> Optional[int]:
    """Number of distinct possible values, or ``None`` if effectively unbounded."""
    if spec.type == "boolean":
        return 2
    if spec.type == "enum":
        return len(spec.choices or [])
    if spec.type == "int" and spec.min_value is not None and spec.max_value is not None:
        return int(spec.max_value) - int(spec.min_value) + 1
    if spec.type == "string" and spec.min_length is not None and spec.max_length is not None:
        return sum(len(_STRING_ALPHABET) ** length
                   for length in range(spec.min_length, spec.max_length + 1))
    if spec.type == "name":
        return _NAME_SPACE
    return None  # float, uuid, email, date, datetime, unbounded int/string


def _random_string(rng: random.Random, spec: FieldSpec) -> str:
    lo = spec.min_length if spec.min_length is not None else _DEFAULT_STR_MIN
    hi = spec.max_length if spec.max_length is not None else _DEFAULT_STR_MAX
    length = rng.randint(lo, hi)
    body = "".join(rng.choice(_STRING_ALPHABET) for _ in range(length))
    return f"{spec.prefix}{body}" if spec.prefix else body


def _random_value(rng: random.Random, spec: FieldSpec) -> Any:
    t = spec.type
    if t == "int":
        lo = int(spec.min_value) if spec.min_value is not None else _DEFAULT_INT_MIN
        hi = int(spec.max_value) if spec.max_value is not None else _DEFAULT_INT_MAX
        return rng.randint(lo, hi)
    if t == "float":
        lo = spec.min_value if spec.min_value is not None else 0.0
        hi = spec.max_value if spec.max_value is not None else 1000.0
        return round(rng.uniform(lo, hi), _FLOAT_PRECISION)
    if t == "boolean":
        return rng.choice([True, False])
    if t == "string":
        return _random_string(rng, spec)
    if t == "name":
        return f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
    if t == "email":
        slug = "".join(rng.choice(_STRING_ALPHABET[:26]) for _ in range(7))
        local = f"{spec.prefix}{slug}" if spec.prefix else slug
        return f"{local}@{_EMAIL_DOMAIN}"
    if t == "uuid":
        return str(uuid.UUID(int=rng.getrandbits(128), version=4))
    if t == "date":
        return (date(2000, 1, 1) + timedelta(days=rng.randint(0, _DATE_SPAN_DAYS - 1))).isoformat()
    if t == "datetime":
        dt = datetime(2000, 1, 1) + timedelta(seconds=rng.randint(
            0, _DATE_SPAN_DAYS * 86400 - 1))
        return dt.isoformat(timespec="seconds")
    if t == "enum":
        return rng.choice(spec.choices)  # type: ignore[arg-type]
    raise ValueError(f"unsupported field type: {t}")


def _table_index(req: FixtureRequest, name: str) -> int:
    return next(i for i, t in enumerate(req.tables) if t.name == name)


def _gen_table(
    table: TableSpec,
    table_idx: int,
    req: FixtureRequest,
    pools: dict[str, dict[str, list[Any]]],
) -> list[dict[str, Any]]:
    seed = req.seed
    fk_by_col = {fk.column: fk for fk in table.foreign_keys}

    # FK columns that were not declared as fields are synthesised from the
    # target column so the output carries a correctly typed value.
    effective_fields: dict[str, FieldSpec] = dict(table.fields)
    by_name = {t.name: t for t in req.tables}
    for fk in table.foreign_keys:
        if fk.column not in effective_fields:
            target_spec = by_name[fk.references_table].fields[fk.references_column]
            effective_fields[fk.column] = target_spec.model_copy(
                update={"unique": False, "nullable": False}
            )

    # --- feasibility checks for unique columns ----------------------------
    unique_cols = {c for c, s in effective_fields.items() if s.unique}
    for col in unique_cols:
        if col in fk_by_col:
            fk = fk_by_col[col]
            if fk.references_table != table.name:
                parent_rows = by_name[fk.references_table].rows
                if parent_rows < table.rows:
                    raise FixtureValidationError([
                        {
                            "loc": ["body", "tables", str(table_idx), "fields", col],
                            "type": "unique_space_exhausted",
                            "msg": (
                                f"唯一外键列 {col!r} 需要 {table.rows} 个不同值，"
                                f"但目标表 {fk.references_table!r} 只有 "
                                f"{parent_rows} 行可引用"
                            ),
                        }
                    ])
            continue
        size = _domain_size(effective_fields[col])
        if size is not None and size < table.rows:
            raise FixtureValidationError([
                {
                    "loc": ["body", "tables", str(table_idx), "fields", col],
                    "type": "unique_space_exhausted",
                    "msg": (
                        f"唯一列 {col!r} 的取值空间仅 {size} 种，"
                        f"不足以生成 {table.rows} 行不重复数据"
                    ),
                }
            ])

    for uc_i, uc in enumerate(table.unique_constraints):
        sizes = [_domain_size(effective_fields[c]) for c in uc]
        if all(s is not None for s in sizes):
            product = 1
            for s in sizes:
                product *= s  # type: ignore[operator]
            if product < table.rows:
                raise FixtureValidationError([
                    {
                        "loc": ["body", "tables", str(table_idx),
                                "unique_constraints", str(uc_i)],
                        "type": "unique_space_exhausted",
                        "msg": (
                            f"复合唯一约束 {uc} 的取值组合仅 {product} 种，"
                            f"不足以生成 {table.rows} 行不重复数据"
                        ),
                    }
                ])

    # --- generators --------------------------------------------------------
    rngs = {
        col: child_rng(seed, table.name, col)
        for col in effective_fields
    }
    fk_rngs = {
        fk.column: child_rng(seed, table.name, fk.column, "fk")
        for fk in table.foreign_keys
    }

    seen: dict[str, set[Any]] = {c: set() for c in unique_cols}
    composite_seen: list[tuple[tuple[str, ...], set[tuple]]] = [
        (tuple(uc), set()) for uc in table.unique_constraints
    ]

    self_pools = pools.setdefault(table.name, {})

    def resolve_fk(column: str) -> Any:
        fk = fk_by_col[column]
        if fk.references_table == table.name:
            pool = self_pools.setdefault(fk.references_column, [])
        else:
            pool = pools[fk.references_table].get(fk.references_column, [])
        if not pool:
            if fk.nullable:
                return None
            raise FixtureValidationError([
                {
                    "loc": ["body", "tables", str(table_idx), "foreign_keys"],
                    "type": "empty_fk_target",
                    "msg": (
                        f"外键 {table.name}.{fk.column} -> "
                        f"{fk.references_table}.{fk.references_column} "
                        "没有可引用的记录，且 nullable=false"
                        + ("（自引用表的第一行必须允许为 null）"
                           if fk.references_table == table.name else "")
                    ),
                }
            ])
        return fk_rngs[column].choice(pool)

    rows_out: list[dict[str, Any]] = []
    max_attempts = max(1000, table.rows * 20)

    for _ in range(table.rows):
        for _attempt in range(max_attempts):
            row: dict[str, Any] = {}
            for col, spec in effective_fields.items():
                if col in fk_by_col:
                    row[col] = resolve_fk(col)
                elif spec.nullable and rngs[col].random() < _NULL_RATE:
                    row[col] = None
                else:
                    row[col] = _random_value(rngs[col], spec)

            collision = any(
                row[c] is not None and row[c] in seen[c]
                for c in unique_cols
            )
            if not collision:
                for cols_tuple, used in composite_seen:
                    key = tuple(row[c] for c in cols_tuple)
                    if None not in key and key in used:
                        collision = True
                        break

            if not collision:
                break
        else:
            raise FixtureValidationError([
                {
                    "loc": ["body", "tables", str(table_idx)],
                    "type": "unique_space_exhausted",
                    "msg": f"表 {table.name!r} 在 {max_attempts} 次尝试后仍无法满足唯一约束",
                }
            ])

        # Commit the row.
        rows_out.append(row)
        for c in unique_cols:
            if row[c] is not None:
                seen[c].add(row[c])
        for cols_tuple, used in composite_seen:
            key = tuple(row[c] for c in cols_tuple)
            if None not in key:
                used.add(key)
        # Self-referential FKs may point at any earlier row from now on.
        for fk in table.foreign_keys:
            if fk.references_table == table.name:
                self_pools.setdefault(fk.references_column, []).append(
                    row[fk.references_column]
                )

    # Publish this table's unique columns for downstream FKs.
    published = pools.setdefault(table.name, {})
    for col, spec in effective_fields.items():
        if spec.unique:
            published.setdefault(col, [r[col] for r in rows_out if r[col] is not None])
    return rows_out


def generate_all(req: FixtureRequest, plan: Plan) -> dict[str, list[dict[str, Any]]]:
    """Generate every table in dependency order; raise 422 on any conflict."""
    by_name = {t.name: t for t in req.tables}
    pools: dict[str, dict[str, list[Any]]] = {}
    generated: dict[str, list[dict[str, Any]]] = {}

    # Topological order guarantees every parent table is fully populated
    # before a child starts drawing FK values.
    for name in plan.order:
        table = by_name[name]
        idx = _table_index(req, name)
        generated[name] = _gen_table(table, idx, req, pools)

    # Return tables in the order the client declared them.
    return {t.name: generated[t.name] for t in req.tables}
