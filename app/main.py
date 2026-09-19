"""FastAPI 入口：多人文件交接留痕（哈希链）服务。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import config, database
from .schemas import (
    AcceptOfferRequest,
    ChainOut,
    ChainSummary,
    CreateChainRequest,
    CreateOfferRequest,
    HandoverRequest,
    HandoverResult,
    HealthOut,
    OfferAcceptResult,
    OfferCreatedOut,
    OfferOut,
    VerifyResult,
)
from .service import (
    ChainError,
    accept_offer,
    append_handover,
    create_chain,
    create_offer,
    get_chain,
    get_offer,
    list_chains,
    verify_chain,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动即完成数据库初始化（建表 / 校验目录）
    database.init_db()
    yield


app = FastAPI(
    title="文件交接留痕服务",
    description="基于 SHA-256 哈希链的多人文件交接记录、查询与完整性校验。",
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(ChainError)
async def chain_error_handler(request: Request, exc: ChainError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "detail": exc.message},
    )


@app.get("/health", response_model=HealthOut, tags=["meta"], summary="健康检查")
def health() -> HealthOut:
    return HealthOut(status="ok")


@app.post(
    "/chains",
    response_model=ChainOut,
    status_code=201,
    tags=["chains"],
    summary="创建交接链",
    description="提交文件 SHA-256 摘要与首位持有人，生成序号 0 的创世记录并返回其哈希。",
)
def api_create_chain(req: CreateChainRequest) -> ChainOut:
    return ChainOut(**create_chain(req.file_hash, req.first_holder))


@app.post(
    "/handovers",
    response_model=HandoverResult,
    status_code=201,
    tags=["chains"],
    summary="执行一次移交（原子追加）",
    description=(
        "必须携带当前链尾哈希 tail_hash、接收方 receiver 与一次性随机数 nonce。"
        "交出方由服务端按当前持有人确定。链尾不匹配或随机数重放返回 409，不产生分叉。"
    ),
)
def api_handover(req: HandoverRequest) -> HandoverResult:
    return HandoverResult(
        **append_handover(req.chain_id, req.tail_hash, req.receiver, req.nonce)
    )


@app.post(
    "/offers",
    response_model=OfferCreatedOut,
    status_code=201,
    tags=["offers"],
    summary="发起待接收移交邀约",
    description=(
        "以最新链尾 tail_hash 发起邀约，指定接收方 receiver、有效期 ttl_seconds "
        "与一次性确认码 confirm_code。服务端只保存确认码的加盐 SHA-256 摘要，"
        "不保存明文；返回 offer_id 与到期时间。链尾不匹配返回 409。"
    ),
)
def api_create_offer(req: CreateOfferRequest) -> OfferCreatedOut:
    return OfferCreatedOut(
        **create_offer(
            req.chain_id,
            req.tail_hash,
            req.receiver,
            req.ttl_seconds,
            req.confirm_code,
        )
    )


@app.post(
    "/offers/{offer_id}/accept",
    response_model=OfferAcceptResult,
    tags=["offers"],
    summary="确认并完成待接收移交",
    description=(
        "接收方凭 offer_id 与一次性确认码确认。服务端在同一个 SQLite 立即事务中"
        "校验邀约未过期、未被使用且链尾仍匹配，然后追加唯一移交事件并将邀约置为 "
        "accepted。过期（offer_expired）、重复确认（offer_already_accepted）、"
        "链尾已推进（tail_mismatch）或确认码错误（offer_code_mismatch）均返回 "
        "409 且不写入任何事件。"
    ),
)
def api_accept_offer(offer_id: str, req: AcceptOfferRequest) -> OfferAcceptResult:
    return OfferAcceptResult(**accept_offer(offer_id, req.confirm_code))


@app.get(
    "/offers/{offer_id}",
    response_model=OfferOut,
    tags=["offers"],
    summary="查询单个邀约状态",
    description=(
        "返回邀约状态 pending / accepted / expired、到期时间及接受后追加的事件序号；"
        "不会返回确认码明文或摘要。"
    ),
)
def api_get_offer(offer_id: str) -> OfferOut:
    return OfferOut(**get_offer(offer_id))


@app.get(
    "/chains",
    response_model=list[ChainSummary],
    tags=["chains"],
    summary="列出全部交接链",
)
def api_list_chains() -> list[ChainSummary]:
    return [ChainSummary(**c) for c in list_chains()]

@app.get(
    "/chains/{chain_id}",
    response_model=ChainOut,
    tags=["chains"],
    summary="查询链条（含全部事件）",
)
def api_get_chain(chain_id: str) -> ChainOut:
    return ChainOut(**get_chain(chain_id))


@app.get(
    "/chains/{chain_id}/verify",
    response_model=VerifyResult,
    tags=["chains"],
    summary="完整性校验",
    description="逐条重算哈希并校验前序指针，结果给出首个断裂序号 first_broken_seq。",
)
def api_verify_chain(chain_id: str) -> VerifyResult:
    return VerifyResult(**verify_chain(chain_id))


def main() -> None:
    """本地直接运行入口（python -m app.main）。"""
    import uvicorn

    database.init_db()
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT)


if __name__ == "__main__":
    main()
