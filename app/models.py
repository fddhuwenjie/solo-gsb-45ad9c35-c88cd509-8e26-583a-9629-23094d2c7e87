"""Pydantic schemas for the fixture-generation API."""

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

FIELD_TYPES = (
    "int",
    "float",
    "boolean",
    "string",
    "name",
    "email",
    "uuid",
    "date",
    "datetime",
    "enum",
)
FieldType = Literal[
    "int",
    "float",
    "boolean",
    "string",
    "name",
    "email",
    "uuid",
    "date",
    "datetime",
    "enum",
]


class FieldSpec(BaseModel):
    """Definition of one column."""

    model_config = ConfigDict(extra="forbid")

    type: FieldType = Field(description="数据类型")
    unique: bool = Field(default=False, description="该列是否唯一（单列唯一约束）")
    nullable: bool = Field(default=False, description="是否允许生成 null（约 10% 概率）")
    min_value: Optional[float] = Field(default=None, description="int/float 的下界")
    max_value: Optional[float] = Field(default=None, description="int/float 的上界")
    min_length: Optional[int] = Field(default=None, ge=0, description="string 最小长度")
    max_length: Optional[int] = Field(default=None, ge=1, description="string 最大长度")
    choices: Optional[list[str]] = Field(
        default=None, description="enum 类型的可选值列表（非空）"
    )
    prefix: Optional[str] = Field(default=None, description="string/email 的前缀")

    @model_validator(mode="after")
    def _validate_params(self) -> "FieldSpec":
        if (
            self.min_value is not None
            and self.max_value is not None
            and self.min_value > self.max_value
        ):
            raise ValueError("min_value 必须小于或等于 max_value")
        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise ValueError("min_length 必须小于或等于 max_length")
        if self.type == "enum" and not self.choices:
            raise ValueError("type 为 enum 时必须提供非空的 choices")
        if self.type != "enum" and self.choices is not None:
            raise ValueError("choices 仅支持 type 为 enum 的字段")
        if self.type not in ("int", "float") and (
            self.min_value is not None or self.max_value is not None
        ):
            raise ValueError("min_value/max_value 仅支持 int、float 字段")
        if self.type != "string" and (
            self.min_length is not None or self.max_length is not None
        ):
            raise ValueError("min_length/max_length 仅支持 string 字段")
        if self.prefix is not None and self.type not in ("string", "email"):
            raise ValueError("prefix 仅支持 string、email 字段")
        return self


class ForeignKey(BaseModel):
    """A foreign-key constraint on one column."""

    model_config = ConfigDict(extra="forbid")

    column: str = Field(min_length=1, description="本表中的外键列名")
    references_table: str = Field(min_length=1, description="被引用的目标表名")
    references_column: str = Field(
        default="id", min_length=1, description="被引用的目标列（默认 id，必须唯一）"
    )
    nullable: bool = Field(
        default=True, description="目标表没有可引用记录时是否允许写成 null"
    )


class TableSpec(BaseModel):
    """Definition of one table."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, description="表名")
    rows: int = Field(ge=0, description="需要生成的行数")
    fields: dict[str, FieldSpec] = Field(min_length=1, description="字段定义")
    foreign_keys: list[ForeignKey] = Field(default_factory=list, description="外键列表")
    unique_constraints: list[list[str]] = Field(
        default_factory=list, description="复合唯一约束，每项为字段名列表"
    )


class FixtureRequest(BaseModel):
    """POST /fixtures request body."""

    model_config = ConfigDict(extra="forbid")

    tables: list[TableSpec] = Field(min_length=1, description="表结构定义")
    seed: Union[int, str] = Field(
        default=42, description="随机种子；相同输入与 seed 生成结果完全一致"
    )


class FixtureResponse(BaseModel):
    """POST /fixtures response body."""

    seed: Union[int, str]
    row_counts: dict[str, int]
    tables: dict[str, list[dict[str, Any]]]


class SensitiveColumn(BaseModel):
    """One anonymization rule: the column named here gets pseudonymized."""

    model_config = ConfigDict(extra="forbid")

    table: str = Field(min_length=1, description="表名，必须存在于表结构中")
    column: str = Field(min_length=1, description="待脱敏列名，必须存在于该表中")


class AnonymizeRequest(BaseModel):
    """POST /fixtures/anonymize request body."""

    model_config = ConfigDict(extra="forbid")

    fixture: FixtureResponse = Field(description="一份 /fixtures 响应（原始 fixture）")
    tables: list[TableSpec] = Field(min_length=1, description="与 fixture 对应的表结构定义")
    sensitive_columns: list[SensitiveColumn] = Field(
        default_factory=list, description="待脱敏列清单；未列出的列原样保留"
    )
    seed: Union[int, str] = Field(
        default=42, description="脱敏种子；相同 fixture、规则与 seed 的脱敏结果完全一致"
    )


class MergeRequest(BaseModel):
    """POST /fixtures/merge request body."""

    model_config = ConfigDict(extra="forbid")

    fixtures: list[FixtureResponse] = Field(
        min_length=2,
        description="两份以上待合并的 fixture，每份为 {seed, row_counts, tables}",
    )
    tables: list[TableSpec] = Field(
        min_length=1,
        description="所有 fixture 共享的表结构定义（同 /fixtures 的 tables）",
    )
    seed: Union[int, str] = Field(
        default=42,
        description="合并种子；相同 fixtures、表结构与 seed 的合并结果完全一致",
    )


class SliceRoot(BaseModel):
    """根表筛选条件：选中根表中 column == value 的行作为闭包起点。"""

    model_config = ConfigDict(extra="forbid")

    table: str = Field(min_length=1, description="根表表名，必须存在于表结构中")
    column: str = Field(min_length=1, description="筛选列名，必须存在于该表中")
    value: Any = Field(
        description=(
            "筛选值，只取类型匹配且严格相等（==）的行；值为 null 时匹配该列为 "
            "null 的行。无任何行命中时，所有表均输出空数组（200）"
        )
    )


class SliceRequest(BaseModel):
    """POST /fixtures/slice request body."""

    model_config = ConfigDict(extra="forbid")

    fixture: FixtureResponse = Field(description="一份 /fixtures 响应（原始 fixture）")
    tables: list[TableSpec] = Field(min_length=1, description="与 fixture 对应的表结构定义")
    root: SliceRoot = Field(description="根表筛选条件（表名、列名和值）")
    limits: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "每表可选行数上限（表名 -> 正整数）；未列出的表不设上限。"
            "上限必须容得下闭包必需的行（根行与其全部父行），否则返回 422"
        ),
    )
    seed: Union[int, str] = Field(
        default=42,
        description="截断种子；相同 fixture、条件、上限与 seed 的裁剪结果完全一致",
    )

    @model_validator(mode="after")
    def _validate_limits(self) -> "SliceRequest":
        for table, limit in self.limits.items():
            if limit < 1:
                raise ValueError(f"表 {table!r} 的行数上限必须为正整数")
        return self
