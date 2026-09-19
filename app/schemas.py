"""请求 / 响应模型与输入校验。"""
from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel, Field, field_validator

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_NAME_MAX = 200
_NONCE_MAX = 256
_CODE_MAX = 256
# 邀约有效期（秒）允许范围：至少 1 秒，至多 7 天
_TTL_MIN = 1
_TTL_MAX = 7 * 24 * 3600


def _validate_name(value: str, field: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{field} 不能为空")
    if len(value) > _NAME_MAX:
        raise ValueError(f"{field} 长度不能超过 {_NAME_MAX}")
    return value


class CreateChainRequest(BaseModel):
    file_hash: str = Field(..., description="交接文件的 SHA-256 摘要（64 位十六进制）")
    first_holder: str = Field(..., description="首位持有人标识")

    @field_validator("file_hash")
    @classmethod
    def _check_file_hash(cls, v: str) -> str:
        v = v.strip().lower()
        if not _HEX64.match(v):
            raise ValueError("file_hash 必须是 64 位十六进制 SHA-256 摘要")
        return v

    @field_validator("first_holder")
    @classmethod
    def _check_first_holder(cls, v: str) -> str:
        return _validate_name(v, "first_holder")


class HandoverRequest(BaseModel):
    chain_id: str = Field(..., description="交接链 ID")
    tail_hash: str = Field(..., description="客户端认定的当前链尾哈希")
    receiver: str = Field(..., description="接收方标识")
    nonce: str = Field(..., description="一次性随机数，同一链内不可重复（防重放）")

    @field_validator("chain_id")
    @classmethod
    def _check_chain_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("chain_id 不能为空")
        return v

    @field_validator("tail_hash")
    @classmethod
    def _check_tail_hash(cls, v: str) -> str:
        v = v.strip().lower()
        if not _HEX64.match(v):
            raise ValueError("tail_hash 必须是 64 位十六进制哈希")
        return v

    @field_validator("receiver")
    @classmethod
    def _check_receiver(cls, v: str) -> str:
        return _validate_name(v, "receiver")

    @field_validator("nonce")
    @classmethod
    def _check_nonce(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("nonce 不能为空（一次性随机数）")
        if len(v) > _NONCE_MAX:
            raise ValueError(f"nonce 长度不能超过 {_NONCE_MAX}")
        return v


class CreateOfferRequest(BaseModel):
    chain_id: str = Field(..., description="交接链 ID")
    tail_hash: str = Field(..., description="发起邀约时认定的当前链尾哈希")
    receiver: str = Field(..., description="邀约的接收方标识")
    ttl_seconds: int = Field(
        ...,
        ge=_TTL_MIN,
        le=_TTL_MAX,
        description="邀约有效期（秒）：1 ~ 604800（7 天），过期后确认一律拒绝",
    )
    confirm_code: str = Field(
        ...,
        description="一次性确认码；服务端只保存其加盐 SHA-256 摘要，不保存明文",
    )

    @field_validator("chain_id")
    @classmethod
    def _check_chain_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("chain_id 不能为空")
        return v

    @field_validator("tail_hash")
    @classmethod
    def _check_tail_hash(cls, v: str) -> str:
        v = v.strip().lower()
        if not _HEX64.match(v):
            raise ValueError("tail_hash 必须是 64 位十六进制哈希")
        return v

    @field_validator("receiver")
    @classmethod
    def _check_receiver(cls, v: str) -> str:
        return _validate_name(v, "receiver")

    @field_validator("confirm_code")
    @classmethod
    def _check_confirm_code(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("confirm_code 不能为空（一次性确认码）")
        if len(v) > _CODE_MAX:
            raise ValueError(f"confirm_code 长度不能超过 {_CODE_MAX}")
        return v


class AcceptOfferRequest(BaseModel):
    confirm_code: str = Field(..., description="发起方线下告知的一次性确认码")

    @field_validator("confirm_code")
    @classmethod
    def _check_confirm_code(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("confirm_code 不能为空")
        if len(v) > _CODE_MAX:
            raise ValueError(f"confirm_code 长度不能超过 {_CODE_MAX}")
        return v


class EventOut(BaseModel):
    seq: int
    prev_hash: str
    giver: str
    receiver: str
    nonce: str
    event_hash: str
    canonical: str = Field(..., description="计算该哈希所用的紧凑 JSON 原文，便于审计复核")
    created_at: str


class ChainOut(BaseModel):
    chain_id: str
    file_hash: str
    first_holder: str
    tail_hash: str
    current_holder: str
    length: int = Field(..., description="事件总数（含创世记录，因此首次创建后为 1）")
    created_at: str
    events: list[EventOut]


class ChainSummary(BaseModel):
    chain_id: str
    file_hash: str
    current_holder: str
    tail_hash: str
    length: int
    created_at: str


class HandoverResult(BaseModel):
    chain_id: str
    seq: int
    prev_hash: str
    giver: str
    receiver: str
    nonce: str
    event_hash: str
    canonical: str
    tail_hash: str
    current_holder: str


class VerifyResult(BaseModel):
    chain_id: str
    valid: bool
    length: int
    expected_next_seq: int
    first_broken_seq: Optional[int] = Field(
        None, description="首个断裂事件的序号；链条头部信息被篡改时为 null"
    )
    reason: Optional[str] = None
    recomputed_tail_hash: Optional[str] = None
    stored_tail_hash: Optional[str] = None


class OfferOut(BaseModel):
    offer_id: str
    chain_id: str
    tail_hash: str = Field(..., description="邀约锁定的链尾；确认时必须仍等于当前链尾")
    receiver: str
    status: str = Field(
        ..., description="邀约状态：pending（待接收）/ accepted（已接受）/ expired（已过期）"
    )
    expires_at: str
    created_at: str
    accepted_at: Optional[str] = Field(None, description="接受时间；未接受时为 null")
    event_seq: Optional[int] = Field(
        None, description="接受后追加的移交事件序号；未接受时为 null"
    )
    is_expired: bool = Field(..., description="当前时刻是否已超过 expires_at")


class OfferCreatedOut(OfferOut):
    # 创建响应额外回传一次确认码，方便发起方核对；服务端存储中从不保留明文
    confirm_code: str = Field(..., description="回显的一次性确认码（仅此一次，后续查询不可见）")


class OfferAcceptResult(BaseModel):
    offer_id: str
    status: str
    chain_id: str
    seq: int
    prev_hash: str
    giver: str
    receiver: str
    nonce: str
    event_hash: str
    canonical: str
    tail_hash: str
    current_holder: str
    accepted_at: str


class HealthOut(BaseModel):
    status: str
