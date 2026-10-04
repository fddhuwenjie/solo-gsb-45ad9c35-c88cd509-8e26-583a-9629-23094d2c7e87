"""FastAPI application: relational test-data generation service."""

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse

from .errors import FixtureValidationError
from .anonymizer import anonymize
from .generator import generate_all
from .merger import merge
from .models import (
    AnonymizeRequest,
    FixtureRequest,
    FixtureResponse,
    MergeRequest,
    SliceRequest,
)
from .planner import build_plan
from .slicer import slice_fixture

app = FastAPI(
    title="关系型测试数据生成 API",
    description=(
        "根据表结构、行数、字段类型、唯一约束、外键和 seed 生成关联的测试数据。  \n\n"
        "- 按外键依赖顺序生成，外键值一定引用已存在的父表记录  \n"
        "- 单列 / 复合唯一约束保证不冲突  \n"
        "- 相同请求体与 seed 结果完全可复现  \n"
        "- `POST /fixtures/anonymize` 可对已有 fixture 做确定性、格式保持的假名化  \n"
        "- `POST /fixtures/slice` 可从已有 fixture 提取可直接运行的关系闭包子集  \n"
        "- `POST /fixtures/merge` 可将多份独立生成的 fixture 合并成一份可直接导入的 fixture  \n"
        "- 依赖成环、引用目标不存在或参数无效时返回 **422**，响应体不含任何生成数据"
    ),
    version="1.3.0",
)


def _validation_payload(errors: list[dict[str, Any]]) -> dict[str, Any]:
    return {"detail": errors}


@app.exception_handler(FixtureValidationError)
async def fixture_validation_exception_handler(
    request: Request, exc: FixtureValidationError
) -> JSONResponse:
    # Same envelope as FastAPI's built-in 422 responses: {"detail": [...]}.
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=_validation_payload(exc.details),
    )


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": jsonable_encoder(exc.errors())},
    )


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["health"], summary="健康检查")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/fixtures",
    response_model=FixtureResponse,
    tags=["fixtures"],
    summary="生成关系型测试数据",
    status_code=status.HTTP_200_OK,
    responses={
        422: {"description": "输入无效 / 依赖成环 / 引用目标不存在（不返回任何部分数据）"},
    },
)
async def create_fixtures(req: FixtureRequest) -> FixtureResponse:
    # All validation happens BEFORE any data is emitted, so a 422 never
    # carries partial results.
    plan = build_plan(req)
    tables = generate_all(req, plan)
    return FixtureResponse(
        seed=req.seed,
        row_counts={name: len(rows) for name, rows in tables.items()},
        tables=tables,
    )


@app.post(
    "/fixtures/anonymize",
    response_model=FixtureResponse,
    tags=["fixtures"],
    summary="对已有 fixture 做确定性、格式保持的假名化",
    status_code=status.HTTP_200_OK,
    responses={
        200: {"description": "脱敏后的 fixture，结构与输入一致"},
        422: {"description": (
            "规则引用未知表/列、类型不支持、唯一约束无法满足或 fixture 与"
            "表结构不一致（不返回任何半成品数据）"
        )},
    },
)
async def anonymize_fixtures(req: AnonymizeRequest) -> FixtureResponse:
    # Validation + anonymization are fully staged inside ``anonymize``:
    # either every value is rewritten consistently or we raise 422.
    return anonymize(
        fixture=req.fixture,
        tables=req.tables,
        sensitive=req.sensitive_columns,
        seed=req.seed,
    )


@app.post(
    "/fixtures/slice",
    response_model=FixtureResponse,
    tags=["fixtures"],
    summary="从已有 fixture 提取可直接运行的关系闭包子集",
    status_code=status.HTTP_200_OK,
    responses={
        200: {"description": "裁剪后的 fixture，未命中的表保留空数组"},
        422: {"description": (
            "上限阻断闭包、根表条件/表结构不合法或原始 unique/FK 已损坏"
            "（不返回任何半成品数据）"
        )},
    },
)
async def slice_fixtures(req: SliceRequest) -> FixtureResponse:
    # The closure is computed and verified in full before anything is
    # returned, so a 422 never carries a partially sliced fixture.
    return slice_fixture(
        fixture=req.fixture,
        tables=req.tables,
        root=req.root,
        limits=req.limits,
        seed=req.seed,
    )


@app.post(
    "/fixtures/merge",
    response_model=FixtureResponse,
    tags=["fixtures"],
    summary="将多份独立生成的 fixture 合并为一份可直接导入的 fixture",
    status_code=status.HTTP_200_OK,
    responses={
        200: {"description": "合并后的 fixture：冲突已按 seed 改写、引用已同步、顺序稳定"},
        422: {"description": (
            "某份 fixture 与表结构不一致、原始 unique/FK 已损坏或唯一约束"
            "无法满足（不返回任何半成品数据）"
        )},
    },
)
async def merge_fixtures(req: MergeRequest) -> FixtureResponse:
    # Every fixture is fully validated before any row is merged, so a 422
    # never carries partially merged data.
    return merge(
        fixtures=req.fixtures,
        tables=req.tables,
        seed=req.seed,
    )
