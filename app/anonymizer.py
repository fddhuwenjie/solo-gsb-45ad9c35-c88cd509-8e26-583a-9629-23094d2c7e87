"""Deterministic, format-preserving pseudonymization of fixture data.

The engine rewrites the sensitive values of an existing fixture response while
keeping everything else unchanged:

* the same original value always maps to the same pseudonym across every table
  and column of the same type (mappings are keyed globally by ``(type, value)``);
* foreign-key columns that are listed for anonymization follow the mapping of
  the column they reference, so parent rows and child rows still point at each
  other; FK columns not listed keep their raw values;
* every pseudonym stays parsable in the original column's type and respects the
  column's constraints (int range/width, string length/prefix, email domain,
  enum choices ...);
* ``None`` is always preserved;
* single-column and composite unique constraints are guaranteed collision free.

All validation (unknown rules, bad types, unsatisfiable uniqueness, ...) runs
before any data is returned, so a 422 never carries half-anonymized results.
"""

import random
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any, Optional, Union

from .errors import FixtureValidationError
from .generator import (
    DATE_SPAN_START,
    FIRST_NAMES,
    LAST_NAMES,
    STRING_ALPHABET,
    child_rng,
)
from .models import FieldSpec, FixtureResponse, ForeignKey, TableSpec
from .planner import Plan, build_plan

_EMAIL_DOMAIN = "example.com"
_FLOAT_PRECISION = 2
_MAX_ROUNDS = 64
# Rejection-sampling attempts before falling back to exhaustive enumeration of
# a small, bounded domain.
_TRY_SMALL = 4000
_TRY_LARGE = 400


# --------------------------------------------------------------------- errors


def _err(
    kind: str,
    msg: str,
    loc: Optional[list[Union[str, int]]] = None,
) -> dict[str, Any]:
    """Build an error envelope matching FastAPI's standard 422 shape."""
    return {"loc": ["body"] + (loc or []), "type": kind, "msg": msg}


def _digits(value: int) -> int:
    return len(str(abs(value)))


# ----------------------------------------------------------------- context


@dataclass
class Context:
    """Intersection of constraints carried by every column sharing one value."""

    spec: FieldSpec
    columns: list[str] = field(default_factory=list)
    int_lo: Optional[int] = None
    int_hi: Optional[int] = None
    str_min: Optional[int] = None
    str_max: Optional[int] = None
    prefix: Optional[str] = None
    enum_values: Optional[set[str]] = None


def _merge_context(ctx: Context, spec: FieldSpec, where: list) -> None:
    """Intersect a freshly encountered column's constraints into ``ctx``."""

    def fail(msg: str) -> None:
        raise FixtureValidationError([_err("unsupported_type", msg, where)])

    if spec.type == "int":
        lo = int(spec.min_value) if spec.min_value is not None else None
        hi = int(spec.max_value) if spec.max_value is not None else None
        if lo is not None:
            ctx.int_lo = lo if ctx.int_lo is None else max(ctx.int_lo, lo)
        if hi is not None:
            ctx.int_hi = hi if ctx.int_hi is None else min(ctx.int_hi, hi)
    elif spec.type == "string":
        if spec.min_length is not None:
            ctx.str_min = (
                spec.min_length
                if ctx.str_min is None
                else max(ctx.str_min, spec.min_length)
            )
        if spec.max_length is not None:
            ctx.str_max = (
                spec.max_length
                if ctx.str_max is None
                else min(ctx.str_max, spec.max_length)
            )
        if spec.prefix is not None:
            if ctx.prefix is not None and ctx.prefix != spec.prefix:
                fail(
                    f"同一原值出现在前缀冲突的列中（{ctx.prefix!r} vs "
                    f"{spec.prefix!r}），无法同时保持两列格式"
                )
            ctx.prefix = spec.prefix
    elif spec.type == "email" and spec.prefix is not None:
        if ctx.prefix is not None and ctx.prefix != spec.prefix:
            fail(
                f"同一 email 原值出现在前缀冲突的列中（{ctx.prefix!r} vs "
                f"{spec.prefix!r}），无法同时保持两列格式"
            )
        ctx.prefix = spec.prefix
    elif spec.type == "enum":
        values = set(spec.choices or [])
        ctx.enum_values = values if ctx.enum_values is None else ctx.enum_values & values


# ------------------------------------------------------- value conformance


def _conformance_error(
    value: Any, spec: FieldSpec, loc: list
) -> Optional[dict[str, Any]]:
    """Check that a value present in the fixture matches its declared type."""
    t = spec.type
    if value is None:
        return None

    def bad(msg: str) -> dict[str, Any]:
        return _err("unsupported_type", msg, loc)

    if t == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            return bad(f"值 {value!r} 不是可解析的 int")
        if spec.min_value is not None and value < int(spec.min_value):
            return bad(f"值 {value} 小于下界 {int(spec.min_value)}")
        if spec.max_value is not None and value > int(spec.max_value):
            return bad(f"值 {value} 大于上界 {int(spec.max_value)}")
    elif t == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return bad(f"值 {value!r} 不是可解析的 float")
    elif t == "boolean":
        if not isinstance(value, bool):
            return bad(f"值 {value!r} 不是可解析的 boolean")
    elif t == "string":
        if not isinstance(value, str):
            return bad(f"值 {value!r} 不是可解析的 string")
        body_len = len(value) - (len(spec.prefix) if spec.prefix else 0)
        if spec.min_length is not None and body_len < spec.min_length:
            return bad(f"字符串 {value!r} 的随机部分短于 min_length={spec.min_length}")
        if spec.max_length is not None and body_len > spec.max_length:
            return bad(f"字符串 {value!r} 的随机部分长于 max_length={spec.max_length}")
        if spec.prefix and not value.startswith(spec.prefix):
            return bad(f"字符串 {value!r} 缺少前缀 {spec.prefix!r}")
    elif t == "name":
        if not isinstance(value, str) or len(value.split()) < 2:
            return bad(f"值 {value!r} 不是可解析的 name（应形如 'First Last'）")
    elif t == "email":
        if not isinstance(value, str) or value.count("@") != 1:
            return bad(f"值 {value!r} 不是可解析的 email")
    elif t == "uuid":
        if not isinstance(value, str):
            return bad(f"值 {value!r} 不是可解析的 uuid")
        try:
            uuid.UUID(value)
        except (ValueError, AttributeError, TypeError):
            return bad(f"值 {value!r} 不是可解析的 uuid")
    elif t == "date":
        if not isinstance(value, str):
            return bad(f"值 {value!r} 不是可解析的 date")
        try:
            date.fromisoformat(value)
        except ValueError:
            return bad(f"值 {value!r} 不是可解析的 date（YYYY-MM-DD）")
    elif t == "datetime":
        if not isinstance(value, str):
            return bad(f"值 {value!r} 不是可解析的 datetime")
        try:
            datetime.fromisoformat(value)
        except ValueError:
            return bad(f"值 {value!r} 不是可解析的 datetime（ISO 8601）")
    elif t == "enum":
        if value not in (spec.choices or []):
            return bad(f"枚举值 {value!r} 不在 choices {spec.choices!r} 中")
    else:  # pragma: no cover - FIELD_TYPES exhausts the literal
        return bad(f"类型 {t!r} 不支持脱敏")
    return None


# ------------------------------------------------------- candidate drawing


def _int_band(value: int, ctx: Context) -> tuple[int, int]:
    """Integer domain for one value: declared bounds intersected with the
    value's own sign and decimal width, so the pseudonym keeps the same shape.
    """
    width = _digits(value)
    if value < 0:
        band_lo, band_hi = -(10**width - 1), -(10 ** (width - 1))
    else:
        band_lo, band_hi = 10 ** (width - 1), 10**width - 1
        if width == 1:
            band_lo = 0
    lo = band_lo if ctx.int_lo is None else max(band_lo, ctx.int_lo)
    hi = band_hi if ctx.int_hi is None else min(band_hi, ctx.int_hi)
    return lo, hi


def _pick_int(
    rng: random.Random, value: int, ctx: Context, used: set
) -> Optional[int]:
    lo, hi = _int_band(value, ctx)
    if lo > hi:
        # Pre-flight infeasibility is reported by _infeasible_contexts, where
        # the location list is assembled cleanly.
        return None
    span = hi - lo + 1
    tries = _TRY_SMALL if span <= _TRY_SMALL else _TRY_LARGE
    for _ in range(tries):
        cand = rng.randint(lo, hi)
        if cand not in used:
            return cand
    for cand in range(lo, hi + 1):  # exhaustive fallback for tight domains
        if cand not in used:
            return cand
    return None


def _decimal_order(value: float) -> int:
    """Order of magnitude (power of ten) of the value's decimal digits."""
    text = format(abs(float(value)), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return len(text.replace(".", "").lstrip("0") or "0")


def _pick_float(rng: random.Random, value: float, used: set) -> Optional[float]:
    bound = 10 ** _decimal_order(value)
    if value < 0:
        lo, hi = -bound, -0.01
    elif value > 0:
        lo, hi = 0.01, bound
    else:
        lo, hi = -1.0, 1.0
    for _ in range(_TRY_LARGE):
        cand = round(rng.uniform(lo, hi), _FLOAT_PRECISION)
        if cand not in used:
            return cand
    return None


def _pick_boolean(rng: random.Random, used: set) -> Optional[bool]:
    free = {True, False} - used
    if not free:
        return None
    return rng.choice(sorted(free, key=lambda b: int(b)))


def _pick_string(
    rng: random.Random, value: str, ctx: Context, used: set
) -> Optional[str]:
    length = len(value) - (len(ctx.prefix) if ctx.prefix else 0)
    if ctx.str_min is not None:
        length = max(length, ctx.str_min)
    if ctx.str_max is not None:
        length = min(length, ctx.str_max)
    if length < 0:
        return None
    alphabet = STRING_ALPHABET
    base = len(alphabet)
    domain = base**length

    def render(index: int) -> str:
        chars: list[str] = []
        for _ in range(length):
            index, rem = divmod(index, base)
            chars.append(alphabet[rem])
        body = "".join(chars)
        return f"{ctx.prefix}{body}" if ctx.prefix else body

    tries = _TRY_SMALL if domain <= _TRY_SMALL else _TRY_LARGE
    for _ in range(tries):
        cand = render(rng.randrange(domain))
        if cand not in used:
            return cand
    if domain <= _TRY_SMALL:  # never fail spuriously on a small domain
        for index in range(domain):
            cand = render(index)
            if cand not in used:
                return cand
    return None


def _pick_name(rng: random.Random, used: set) -> Optional[str]:
    for _ in range(_TRY_LARGE):
        cand = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
        if cand not in used:
            return cand
    for first in FIRST_NAMES:  # exhaustive fallback (name space is finite)
        for last in LAST_NAMES:
            cand = f"{first} {last}"
            if cand not in used:
                return cand
    return None


def _pick_email(
    rng: random.Random, value: str, ctx: Context, used: set
) -> Optional[str]:
    domain = value.rsplit("@", 1)[1] if "@" in value else _EMAIL_DOMAIN
    for _ in range(_TRY_LARGE):
        slug = "".join(rng.choice(STRING_ALPHABET[:26]) for _ in range(10))
        local = f"{ctx.prefix}{slug}" if ctx.prefix else slug
        cand = f"{local}@{domain}"
        if cand not in used:
            return cand
    return None


def _pick_uuid(rng: random.Random, used: set) -> Optional[str]:
    for _ in range(_TRY_LARGE):
        cand = str(uuid.UUID(int=rng.getrandbits(128), version=4))
        if cand not in used:
            return cand
    return None


def _pick_date(rng: random.Random, used: set) -> Optional[str]:
    start = DATE_SPAN_START.toordinal()
    for _ in range(_TRY_LARGE):
        cand = date.fromordinal(start + rng.randint(0, 365 * 30 - 1)).isoformat()
        if cand not in used:
            return cand
    return None


def _pick_datetime(rng: random.Random, used: set) -> Optional[str]:
    start = int(datetime.combine(DATE_SPAN_START, datetime.min.time()).timestamp())
    for _ in range(_TRY_LARGE):
        cand = datetime.fromtimestamp(
            start + rng.randint(0, 365 * 30 * 86400 - 1)
        ).isoformat(timespec="seconds")
        if cand not in used:
            return cand
    return None


def _pick_enum(rng: random.Random, ctx: Context, used: set) -> Optional[str]:
    free = [c for c in sorted(ctx.enum_values or set()) if c not in used]
    if not free:
        return None
    return rng.choice(free)


_TYPES = ("int", "float", "boolean", "string", "name", "email", "uuid",
          "date", "datetime", "enum")


def _draw(
    rng: random.Random,
    value: Any,
    ctx: Context,
    used_by_type: dict[str, set],
) -> Optional[Any]:
    t = ctx.spec.type
    used = used_by_type[t]
    if t == "int":
        return _pick_int(rng, value, ctx, used)
    if t == "float":
        return _pick_float(rng, value, used)
    if t == "boolean":
        return _pick_boolean(rng, used)
    if t == "string":
        return _pick_string(rng, value, ctx, used)
    if t == "name":
        return _pick_name(rng, used)
    if t == "email":
        return _pick_email(rng, value, ctx, used)
    if t == "uuid":
        return _pick_uuid(rng, used)
    if t == "date":
        return _pick_date(rng, used)
    if t == "datetime":
        return _pick_datetime(rng, used)
    if t == "enum":
        return _pick_enum(rng, ctx, used)
    return None  # pragma: no cover


# ------------------------------------------------------------- main entry


@dataclass
class _MappedCol:
    spec: FieldSpec
    fk: Optional[ForeignKey]
    target_spec: Optional[FieldSpec]


def anonymize(
    fixture: FixtureResponse,
    tables: list[TableSpec],
    sensitive: list,
    seed: Union[int, str],
) -> FixtureResponse:
    errors: list[dict[str, Any]] = []
    by_name = {t.name: t for t in tables}
    # build_plan rejects duplicate table names, cycles and unknown FK targets.
    plan = build_plan(SimpleNamespace(tables=tables, seed=seed))

    # -------------------------------------------------- validate the rules
    listed: set[tuple[str, str]] = set()
    fks_by_col: dict[str, dict[str, ForeignKey]] = {
        t.name: {fk.column: fk for fk in t.foreign_keys} for t in tables
    }

    for i, rule in enumerate(sensitive):
        rule_loc = ["sensitive_columns", i]
        if rule.table not in by_name:
            errors.append(_err(
                "unknown_table", f"脱敏规则引用的表 {rule.table!r} 不存在",
                rule_loc + ["table"],
            ))
            continue
        effective = _effective_fields(by_name[rule.table], by_name)
        if rule.column not in effective:
            errors.append(_err(
                "unknown_column",
                f"脱敏规则引用的列 {rule.table}.{rule.column} 不存在",
                rule_loc + ["column"],
            ))
            continue
        key = (rule.table, rule.column)
        if key in listed:
            errors.append(_err(
                "duplicate_rule",
                f"列 {rule.table}.{rule.column} 被重复列入脱敏清单",
                rule_loc,
            ))
        listed.add(key)

    # Anonymizing a parent column forces every FK pointing at it to be listed
    # too; otherwise child rows would keep raw values that no longer exist.
    for t in tables:
        for fk in t.foreign_keys:
            if (fk.references_table, fk.references_column) in listed and (
                t.name,
                fk.column,
            ) not in listed:
                errors.append(_err(
                    "fk_target_anonymized",
                    f"列 {t.name}.{fk.column} 是外键，引用了被脱敏的 "
                    f"{fk.references_table}.{fk.references_column}，必须同时列入"
                    "脱敏清单（否则外键会指向不存在的值）",
                ))

    if errors:
        raise FixtureValidationError(errors)

    # ------------------------------------------- validate fixture structure
    # _validate_fixture_shape raises 422 on the first structural problem.
    fixture_tables = fixture.tables
    _validate_fixture_shape(
        fixture,
        by_name,
        plan.order,
        check_row_counts=True,
        conformance=listed,
        conformance_loc_builder=lambda name, r_i, col: [
            "fixture", "tables", name, r_i, col
        ],
    )

    # --------------------------- raw data must satisfy its own constraints
    _verify_raw_integrity(tables, by_name, plan, fixture_tables)

    # --------------------------- classify columns and build value contexts
    mapped_cols: dict[str, dict[str, _MappedCol]] = {}
    for name in plan.order:
        table = by_name[name]
        effective = _effective_fields(table, by_name)
        cols: dict[str, _MappedCol] = {}
        for col, spec in effective.items():
            if (name, col) not in listed:
                continue
            fk = fks_by_col[name].get(col)
            if fk is not None and (
                fk.references_table,
                fk.references_column,
            ) not in listed:
                # Listed FK whose parent keeps raw values: keep raw values too
                # so the foreign key still points at the original target.
                continue
            target_spec: Optional[FieldSpec] = None
            if fk is not None:
                target_spec = _effective_fields(
                    by_name[fk.references_table], by_name
                )[fk.references_column]
                if target_spec.type != spec.type:
                    raise FixtureValidationError([_err(
                        "unsupported_type",
                        f"外键列 {name}.{col} 的类型 {spec.type!r} 与目标列 "
                        f"{fk.references_table}.{fk.references_column} 的类型 "
                        f"{target_spec.type!r} 不一致，无法保持格式地脱敏",
                    )])
            cols[col] = _MappedCol(spec=spec, fk=fk, target_spec=target_spec)
        mapped_cols[name] = cols

    # Global mapping context: (type, original value) -> constraints merged
    # from every column where that exact value shows up.
    contexts: dict[tuple[str, Any], Context] = {}
    for name in plan.order:
        rows = fixture_tables[name]
        for col, mc in mapped_cols[name].items():
            ctx_type = mc.target_spec.type if mc.target_spec else mc.spec.type
            specs = [mc.spec] + ([mc.target_spec] if mc.target_spec else [])
            for r_i, row in enumerate(rows):
                value = row[col]
                if value is None:
                    continue
                key = (ctx_type, value)
                ctx = contexts.get(key)
                if ctx is None:
                    ctx = Context(spec=mc.target_spec or mc.spec)
                    contexts[key] = ctx
                ctx.columns.append(f"{name}.{col}[{r_i}]")
                for s in specs:
                    _merge_context(ctx, s, ["fixture", "tables", name, r_i, col])

    infeasible = _infeasible_contexts(contexts)
    if infeasible:
        raise FixtureValidationError(infeasible)

    # Assign pseudonyms. Per-type used-sets keep the global mapping injective,
    # which makes single/ composite uniqueness hold by construction; rounds
    # only re-roll when the final verification spots a composite collision.
    ordered_keys = list(contexts.keys())
    mapping: dict[tuple[str, Any], Any] = {}
    out: dict[str, list[dict[str, Any]]] = {}
    for round_no in range(_MAX_ROUNDS):
        mapping = {}
        used_by_type: dict[str, set] = {t: set() for t in _TYPES}
        failure: Optional[dict[str, Any]] = None
        for key in ordered_keys:
            t, value = key
            ctx = contexts[key]
            rng = child_rng(seed, "anonymize", t, repr(value), round_no)
            cand = _draw(rng, value, ctx, used_by_type)
            if cand is None:
                failure = _err(
                    "unique_space_exhausted",
                    f"无法为 {t} 原值 {value!r} 分配满足全部唯一约束的假名"
                    "（取值空间已耗尽）",
                )
                break
            mapping[key] = cand
            used_by_type[t].add(cand)
        if failure is not None:
            raise FixtureValidationError([failure])

        out = _apply_mapping(fixture_tables, mapped_cols, mapping)
        if _verify_output_uniqueness(tables, by_name, out) is None:
            break
    else:
        raise FixtureValidationError([_err(
            "unique_space_exhausted",
            f"在 {_MAX_ROUNDS} 轮尝试后仍无法同时满足所有唯一约束",
        )])

    _verify_output_foreign_keys(tables, out)

    return FixtureResponse(
        seed=seed,
        row_counts={name: len(out[name]) for name in out},
        tables=out,
    )


def _infeasible_contexts(
    contexts: dict[tuple[str, Any], Context],
) -> list[dict[str, Any]]:
    """Values whose merged domain cannot hold any legal pseudonym."""
    out: list[dict[str, Any]] = []
    for (t, value), ctx in contexts.items():
        where = "; ".join(ctx.columns)
        if t == "enum" and not ctx.enum_values:
            out.append(_err(
                "unique_space_exhausted",
                f"枚举原值 {value!r} 同时出现在 choices 不相交的列中，"
                "找不到任何合法的假名值",
            ))
        elif t == "int":
            lo, hi = _int_band(value, ctx)
            if lo > hi:
                out.append(_err(
                    "unique_space_exhausted",
                    f"int 原值 {value} 无法在所有引用列的范围与自身位数下"
                    f"找到合法假名（涉及 {where}）",
                ))
        elif t == "string":
            lo = ctx.str_min or 0
            hi = ctx.str_max if ctx.str_max is not None else max(lo, 32)
            if lo > hi:
                out.append(_err(
                    "unique_space_exhausted",
                    f"字符串原值 {value!r} 无法同时满足各列 min/max 长度约束"
                    f"（涉及 {where}）",
                ))
    return out


# ------------------------------------------------------------- plumbing


def _effective_fields(
    table: TableSpec, by_name: dict[str, TableSpec]
) -> dict[str, FieldSpec]:
    """Same field synthesis as the generator: undeclared FK columns inherit
    the target column's type."""
    effective = dict(table.fields)
    for fk in table.foreign_keys:
        if fk.column not in effective:
            target_spec = by_name[fk.references_table].fields[fk.references_column]
            effective[fk.column] = target_spec.model_copy(
                update={"unique": False, "nullable": False}
            )
    return effective


def _validate_fixture_shape(
    fixture: FixtureResponse,
    by_name: dict[str, TableSpec],
    order: list[str],
    *,
    check_row_counts: bool,
    conformance: Optional[set[tuple[str, str]]] = None,
    conformance_loc_builder=None,
) -> None:
    """Shared structural validation for endpoints that consume a fixture.

    Checks (in order, collecting every problem before raising):

    * the fixture's table set matches the schema's table set;
    * every table payload is a list of row objects;
    * when ``check_row_counts`` is set, each table's row count matches both
      the schema's ``rows`` and the fixture's own ``row_counts``;
    * every row carries every effective column (declared + synthesised FK);
    * when ``conformance`` is given, values of the listed ``(table, column)``
      pairs are parseable under their declared type, reported at the location
      produced by ``conformance_loc_builder``.
    """
    errors: list[dict[str, Any]] = []
    fixture_tables = fixture.tables
    if set(fixture_tables) != set(by_name):
        missing = sorted(set(by_name) - set(fixture_tables))
        extra = sorted(set(fixture_tables) - set(by_name))
        errors.append(_err(
            "fixture_shape_mismatch",
            f"fixture 中的表集合与表结构不一致（缺少 {missing}，多出 {extra}）",
            ["fixture", "tables"],
        ))
    for name in order:
        table = by_name[name]
        if name not in fixture_tables:
            continue
        rows = fixture_tables[name]
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            errors.append(_err(
                "fixture_shape_mismatch", f"fixture 表 {name!r} 必须是对象数组",
                ["fixture", "tables", name],
            ))
            continue
        if check_row_counts:
            if len(rows) != table.rows:
                errors.append(_err(
                    "fixture_shape_mismatch",
                    f"fixture 表 {name!r} 有 {len(rows)} 行，但表结构声明 "
                    f"rows={table.rows}",
                    ["fixture", "tables", name],
                ))
            if fixture.row_counts.get(name) != len(rows):
                errors.append(_err(
                    "fixture_shape_mismatch",
                    f"fixture.row_counts[{name!r}]={fixture.row_counts.get(name)} "
                    f"与实际行数 {len(rows)} 不一致",
                    ["fixture", "row_counts", name],
                ))
        effective = _effective_fields(table, by_name)
        for r_i, row in enumerate(rows):
            for col in effective:
                if col not in row:
                    errors.append(_err(
                        "fixture_shape_mismatch",
                        f"fixture 表 {name!r} 第 {r_i} 行缺少列 {col!r}",
                        ["fixture", "tables", name, r_i, col],
                    ))
                    continue
                # Only listed values have to be parseable; unlisted columns
                # are copied verbatim regardless of strict typing.
                if conformance is not None and (name, col) in conformance:
                    problem = _conformance_error(
                        row[col], effective[col],
                        conformance_loc_builder(name, r_i, col),
                    )
                    if problem is not None:
                        errors.append(problem)
    if errors:
        raise FixtureValidationError(errors)


def _verify_raw_integrity(
    tables: list[TableSpec],
    by_name: dict[str, TableSpec],
    plan: Plan,
    data: dict[str, list[dict[str, Any]]],
) -> None:
    errors: list[dict[str, Any]] = []
    for name in plan.order:
        table = by_name[name]
        effective = _effective_fields(table, by_name)
        rows = data[name]
        for col, spec in effective.items():
            if not spec.unique:
                continue
            seen: set = set()
            for r_i, row in enumerate(rows):
                v = row.get(col)
                if v is None:  # SQL semantics: NULLs never collide
                    continue
                if v in seen:
                    errors.append(_err(
                        "fixture_unique_violation",
                        f"原始 fixture 的唯一列 {name}.{col} 已存在重复值 {v!r}",
                        ["fixture", "tables", name, r_i, col],
                    ))
                    break
                seen.add(v)
        for uc in table.unique_constraints:
            seen_pairs: set = set()
            for r_i, row in enumerate(rows):
                key = tuple(row.get(c) for c in uc)
                if None in key:
                    continue
                if key in seen_pairs:
                    errors.append(_err(
                        "fixture_unique_violation",
                        f"原始 fixture 在表 {name!r} 的复合唯一约束 {uc} 上"
                        f"存在重复 {key}",
                        ["fixture", "tables", name, r_i],
                    ))
                    break
                seen_pairs.add(key)
    for name in plan.order:
        table = by_name[name]
        for fk in table.foreign_keys:
            pool = {
                r[fk.references_column]
                for r in data[fk.references_table]
                if r.get(fk.references_column) is not None
            }
            for r_i, row in enumerate(data[name]):
                v = row.get(fk.column)
                if v is None:
                    continue
                if v not in pool:
                    errors.append(_err(
                        "fixture_fk_violation",
                        f"原始 fixture 的外键 {name}.{fk.column}={v!r} 在 "
                        f"{fk.references_table}.{fk.references_column} 中"
                        "没有对应记录",
                        ["fixture", "tables", name, r_i, fk.column],
                    ))
                    break
    if errors:
        raise FixtureValidationError(errors)


def _apply_mapping(
    data: dict[str, list[dict[str, Any]]],
    mapped_cols: dict[str, dict[str, _MappedCol]],
    mapping: dict[tuple[str, Any], Any],
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for name, rows in data.items():
        cols = mapped_cols.get(name, {})
        rows_out: list[dict[str, Any]] = []
        for row in rows:
            new_row = dict(row)
            for col, mc in cols.items():
                value = row.get(col)
                if value is None:
                    continue
                t = mc.target_spec.type if mc.target_spec else mc.spec.type
                new_row[col] = mapping[(t, value)]
            rows_out.append(new_row)
        out[name] = rows_out
    return out


def _verify_output_uniqueness(
    tables: list[TableSpec],
    by_name: dict[str, TableSpec],
    out: dict[str, list[dict[str, Any]]],
) -> Optional[dict[str, Any]]:
    for table in tables:
        name = table.name
        effective = _effective_fields(table, by_name)
        rows = out[name]
        for col, spec in effective.items():
            if not spec.unique:
                continue
            seen: set = set()
            for row in rows:
                v = row.get(col)
                if v is None:
                    continue
                if v in seen:
                    return _err(
                        "unique_space_exhausted",
                        f"脱敏后唯一列 {name}.{col} 出现重复值 {v!r}",
                    )
                seen.add(v)
        for uc in table.unique_constraints:
            seen_pairs: set = set()
            for row in rows:
                key = tuple(row.get(c) for c in uc)
                if None in key:
                    continue
                if key in seen_pairs:
                    return _err(
                        "unique_space_exhausted",
                        f"脱敏后表 {name!r} 的复合唯一约束 {uc} 出现重复 {key}",
                    )
                seen_pairs.add(key)
    return None


def _verify_output_foreign_keys(
    tables: list[TableSpec],
    out: dict[str, list[dict[str, Any]]],
) -> None:
    errors: list[dict[str, Any]] = []
    for table in tables:
        name = table.name
        for fk in table.foreign_keys:
            pool = {
                r[fk.references_column]
                for r in out[fk.references_table]
                if r.get(fk.references_column) is not None
            }
            for r_i, row in enumerate(out[name]):
                v = row.get(fk.column)
                if v is None:
                    continue
                if v not in pool:
                    errors.append(_err(
                        "fixture_fk_violation",
                        f"脱敏后外键 {name}.{fk.column}={v!r} 不再指向 "
                        f"{fk.references_table}.{fk.references_column} 中的任何记录",
                    ))
                    break
    if errors:
        raise FixtureValidationError(errors)
