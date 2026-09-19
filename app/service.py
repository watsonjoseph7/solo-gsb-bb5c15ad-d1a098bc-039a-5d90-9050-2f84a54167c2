"""交接链核心业务逻辑。

哈希规则
--------
创世事件（seq=0）：
    canonical = sort_keys_json({"file_hash": ..., "holder": ..., "seq": 0})
    event_hash = SHA256(canonical)
移交事件（seq>=1）：
    canonical = sort_keys_json({
        "giver": 交出方, "nonce": 一次性随机数, "prev_hash": 前序哈希,
        "receiver": 接收方, "seq": 序号,
    })
    event_hash = SHA256(canonical)

交出方由服务端依据链尾持有人（current_holder）确定，客户端不能伪造。

待接收移交（offer）
-------------------
发起方以最新链尾发起邀约，指定 receiver、有效期与一次性确认码。确认码明文
不落库，只保存「每邀约独立盐 + SHA-256」摘要并返回 offer_id。接收方确认时，
在同一个 BEGIN IMMEDIATE 事务内校验：邀约存在 / 未过期 / 仍是 pending /
确认码一致 / 链尾仍匹配，全部通过才追加唯一移交事件并把邀约置为 accepted。
任一条件不满足（过期、重复确认、链尾已推进、确认码错误）一律 409 且不写事件。
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from .database import read_tx, write_tx
from .hashing import canonical_json, digest_hex


class ChainError(Exception):
    """业务错误：携带 HTTP 状态码。"""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _genesis_payload(file_hash: str, holder: str) -> dict[str, Any]:
    return {"file_hash": file_hash, "holder": holder, "seq": 0}


def _handover_payload(
    seq: int, prev_hash: str, giver: str, receiver: str, nonce: str
) -> dict[str, Any]:
    # 键名顺序无所谓 —— canonical_json 会 sort_keys；显式写出仅为可读性。
    return {
        "giver": giver,
        "nonce": nonce,
        "prev_hash": prev_hash,
        "receiver": receiver,
        "seq": seq,
    }


def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "seq": row["seq"],
        "prev_hash": row["prev_hash"],
        "giver": row["giver"],
        "receiver": row["receiver"],
        "nonce": row["nonce"],
        "event_hash": row["event_hash"],
        "canonical": row["canonical"],
        "created_at": row["created_at"],
    }


def create_chain(file_hash: str, first_holder: str) -> dict[str, Any]:
    """创建交接链：写入创世记录（序号 0）。原子完成。"""
    chain_id = uuid.uuid4().hex
    created_at = _now()

    payload = _genesis_payload(file_hash, first_holder)
    canonical = canonical_json(payload)
    genesis_hash = digest_hex(canonical)

    try:
        with write_tx() as conn:
            conn.execute(
                """INSERT INTO chains
                   (chain_id, file_hash, first_holder, tail_hash,
                    current_holder, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (chain_id, file_hash, first_holder, genesis_hash,
                 first_holder, created_at),
            )
            conn.execute(
                """INSERT INTO events
                   (chain_id, seq, prev_hash, giver, receiver, nonce,
                    event_hash, canonical, created_at)
                   VALUES (?, 0, '', '', '', '', ?, ?, ?)""",
                (chain_id, genesis_hash, canonical, created_at),
            )
    except sqlite3.IntegrityError as exc:  # 理论上 UUID 冲突才会走到这里
        raise ChainError(409, "conflict", f"链条创建冲突: {exc}") from exc

    return {
        "chain_id": chain_id,
        "file_hash": file_hash,
        "first_holder": first_holder,
        "tail_hash": genesis_hash,
        "current_holder": first_holder,
        "length": 1,
        "created_at": created_at,
        "events": [
            {
                "seq": 0,
                "prev_hash": "",
                "giver": "",
                "receiver": "",
                "nonce": "",
                "event_hash": genesis_hash,
                "canonical": canonical,
                "created_at": created_at,
            }
        ],
    }


def _append_event_locked(
    conn: sqlite3.Connection,
    chain_id: str,
    expected_tail: str,
    receiver: str,
    nonce: str,
    *,
    tail_mismatch_code: str = "tail_mismatch",
) -> dict[str, Any]:
    """在已开启的立即事务内完成「链尾校验 → 查重 → 追加事件 → 推进链尾」。

    调用方负责 BEGIN IMMEDIATE；本函数不提交，便于与邀约状态更新原子完成。
    """
    chain = conn.execute(
        "SELECT * FROM chains WHERE chain_id = ?", (chain_id,)
    ).fetchone()
    if chain is None:
        raise ChainError(404, "not_found", f"链条不存在: {chain_id}")

    if chain["tail_hash"] != expected_tail:
        raise ChainError(
            409,
            tail_mismatch_code,
            "链尾哈希不匹配：链条已被其他移交推进，请获取最新链尾后重试",
        )

    # UNIQUE(chain_id, nonce) 是最终防线；这里先给出明确错误信息。
    dup = conn.execute(
        "SELECT seq FROM events WHERE chain_id = ? AND nonce = ? AND seq > 0",
        (chain_id, nonce),
    ).fetchone()
    if dup is not None:
        raise ChainError(
            409,
            "nonce_replay",
            f"随机数已被使用（重放被拒绝），首次出现于序号 {dup['seq']}",
        )

    giver = chain["current_holder"]
    last_event = conn.execute(
        "SELECT seq FROM events WHERE chain_id = ? ORDER BY seq DESC LIMIT 1",
        (chain_id,),
    ).fetchone()
    seq = last_event["seq"] + 1

    payload = _handover_payload(
        seq=seq,
        prev_hash=chain["tail_hash"],
        giver=giver,
        receiver=receiver,
        nonce=nonce,
    )
    canonical = canonical_json(payload)
    new_hash = digest_hex(canonical)
    created_at = _now()

    try:
        conn.execute(
            """INSERT INTO events
               (chain_id, seq, prev_hash, giver, receiver, nonce,
                event_hash, canonical, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (chain_id, seq, chain["tail_hash"], giver, receiver, nonce,
             new_hash, canonical, created_at),
        )
    except sqlite3.IntegrityError as exc:
        msg = str(exc)
        if "nonce" in msg:
            raise ChainError(
                409, "nonce_replay", "随机数重放被拒绝（并发请求）"
            ) from exc
        raise ChainError(409, "conflict", f"追加冲突: {msg}") from exc

    conn.execute(
        """UPDATE chains
           SET tail_hash = ?, current_holder = ?
           WHERE chain_id = ?""",
        (new_hash, receiver, chain_id),
    )

    return {
        "chain_id": chain_id,
        "seq": seq,
        "prev_hash": chain["tail_hash"],
        "giver": giver,
        "receiver": receiver,
        "nonce": nonce,
        "event_hash": new_hash,
        "canonical": canonical,
        "tail_hash": new_hash,
        "current_holder": receiver,
    }


def append_handover(
    chain_id: str, tail_hash: str, receiver: str, nonce: str
) -> dict[str, Any]:
    """追加一次移交。

    冲突（409）情形：链条不存在 -> 404；链尾哈希不匹配（过期/竞争请求）；
    随机数在该链内重放。任何冲突都不会写入数据，链条不会分叉。
    """
    with write_tx() as conn:
        result = _append_event_locked(
            conn, chain_id, tail_hash, receiver, nonce
        )
    return result


# ------------------------------------------------------------------
# 待接收移交（offer）
# ------------------------------------------------------------------

def _parse_ts(value: str) -> datetime:
    """解析库内 ISO-8601 UTC 时间（_now 以秒精度写出）。"""
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def _confirm_code_digest(salt: str, confirm_code: str) -> str:
    """确认码摘要：每邀约独立随机盐，SHA-256(salt + ":" + code)。明文不落库。"""
    return hashlib.sha256(f"{salt}:{confirm_code}".encode("utf-8")).hexdigest()


def _offer_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    expired = (
        row["status"] == "pending"
        and datetime.now(timezone.utc) > _parse_ts(row["expires_at"])
    )
    return {
        "offer_id": row["offer_id"],
        "chain_id": row["chain_id"],
        "tail_hash": row["tail_hash"],
        "receiver": row["receiver"],
        "status": "expired" if expired else row["status"],
        "expires_at": row["expires_at"],
        "created_at": row["created_at"],
        "accepted_at": row["accepted_at"] or None,
        "event_seq": row["event_seq"],
        "is_expired": expired or row["status"] == "expired",
    }


def create_offer(
    chain_id: str,
    tail_hash: str,
    receiver: str,
    ttl_seconds: int,
    confirm_code: str,
) -> dict[str, Any]:
    """以最新链尾发起待接收移交邀约。

    服务端只保存确认码的加盐摘要（code_salt/code_digest），绝不保存明文。
    链条不存在 -> 404；链尾已被推进 -> 409（offer_tail_mismatch）。
    """
    offer_id = uuid.uuid4().hex
    nonce = secrets.token_hex(32)
    salt = secrets.token_hex(16)
    code_digest = _confirm_code_digest(salt, confirm_code)

    now = datetime.now(timezone.utc)
    created_at = now.isoformat(timespec="seconds")
    expires_at_ts = int(now.timestamp()) + ttl_seconds
    expires_at = (
        datetime.fromtimestamp(expires_at_ts, tz=timezone.utc)
        .isoformat(timespec="seconds")
    )

    with write_tx() as conn:
        chain = conn.execute(
            "SELECT * FROM chains WHERE chain_id = ?", (chain_id,)
        ).fetchone()
        if chain is None:
            raise ChainError(404, "not_found", f"链条不存在: {chain_id}")

        if chain["tail_hash"] != tail_hash:
            raise ChainError(
                409,
                "offer_tail_mismatch",
                "链尾哈希不匹配：发起邀约前链条已被推进，请获取最新链尾后重试",
            )

        try:
            conn.execute(
                """INSERT INTO offers
                   (offer_id, chain_id, tail_hash, receiver, nonce,
                    code_salt, code_digest, status, expires_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (offer_id, chain_id, tail_hash, receiver, nonce,
                 salt, code_digest, expires_at, created_at),
            )
        except sqlite3.IntegrityError as exc:  # 理论上 UUID 冲突 / 链被删除
            raise ChainError(409, "conflict", f"邀约创建冲突: {exc}") from exc

        row = conn.execute(
            "SELECT * FROM offers WHERE offer_id = ?", (offer_id,)
        ).fetchone()
        result = _offer_to_dict(row)

    # 明文确认码仅在创建响应中回显这一次，后续任何查询都不可见
    result["confirm_code"] = confirm_code
    return result


def accept_offer(offer_id: str, confirm_code: str) -> dict[str, Any]:
    """接收方凭 offer_id + 一次性确认码接受邀约。

    在同一个 BEGIN IMMEDIATE 事务内依次校验：
      1. 邀约存在（否则 404）；
      2. 状态仍为 pending（已接受 -> 409 offer_already_accepted / expired）；
      3. 未超过 expires_at（过期 -> 409 offer_expired，不写事件）；
      4. 确认码摘要一致（错误 -> 409 offer_code_mismatch，不改变邀约状态）；
      5. 邀约锁定的链尾仍是当前链尾（已推进 -> 409 tail_mismatch，不写事件）。
    全部通过后追加唯一移交事件、回写 event_seq，并以条件更新把邀约置为
    accepted；条件更新命中 0 行说明并发重复确认，事务回滚，绝不写出第二条事件。
    """
    with write_tx() as conn:
        offer = conn.execute(
            "SELECT * FROM offers WHERE offer_id = ?", (offer_id,)
        ).fetchone()
        if offer is None:
            raise ChainError(404, "not_found", f"邀约不存在: {offer_id}")

        if offer["status"] == "accepted":
            raise ChainError(
                409,
                "offer_already_accepted",
                f"邀约已被接受（一次性邀约不可重复确认），移交事件序号 "
                f"{offer['event_seq']}",
            )
        if offer["status"] != "pending":
            raise ChainError(
                409, "offer_state", f"邀约当前状态不可确认: {offer['status']}"
            )

        if datetime.now(timezone.utc) > _parse_ts(offer["expires_at"]):
            raise ChainError(
                409,
                "offer_expired",
                f"邀约已于 {offer['expires_at']} 过期，确认被拒绝且不写入事件",
            )

        if not secrets.compare_digest(
            _confirm_code_digest(offer["code_salt"], confirm_code),
            offer["code_digest"],
        ):
            raise ChainError(
                409,
                "offer_code_mismatch",
                "确认码不正确，邀约未被使用（仍可凭正确确认码确认，直至过期）",
            )

        # 链尾校验与事件追加复用既有原子逻辑；邀约指定的 receiver 必须与
        # 发起时锁定的链尾一起兑现，任何中途移交都会让这里失败。
        event = _append_event_locked(
            conn,
            chain_id=offer["chain_id"],
            expected_tail=offer["tail_hash"],
            receiver=offer["receiver"],
            nonce=offer["nonce"],
            tail_mismatch_code="tail_mismatch",
        )

        accepted_at = _now()
        # 最终防线：只有仍是 pending 的邀约才能被置为 accepted
        cur = conn.execute(
            """UPDATE offers
               SET status = 'accepted', accepted_at = ?, event_seq = ?
               WHERE offer_id = ? AND status = 'pending'""",
            (accepted_at, event["seq"], offer_id),
        )
        if cur.rowcount != 1:
            raise ChainError(
                409,
                "offer_already_accepted",
                "邀约已被并发请求接受，本次确认不写入事件",
            )

    return {
        "offer_id": offer_id,
        "status": "accepted",
        "accepted_at": accepted_at,
        **event,
    }


def get_offer(offer_id: str) -> dict[str, Any]:
    """查询单个邀约状态。

    pending 且已过 expires_at 的邀约在读取时惰性标记为 expired 并返回；
    任何情况下都不会返回确认码明文或摘要。
    """
    with write_tx() as conn:
        row = conn.execute(
            "SELECT * FROM offers WHERE offer_id = ?", (offer_id,)
        ).fetchone()
        if row is None:
            raise ChainError(404, "not_found", f"邀约不存在: {offer_id}")

        if (
            row["status"] == "pending"
            and datetime.now(timezone.utc) > _parse_ts(row["expires_at"])
        ):
            conn.execute(
                "UPDATE offers SET status = 'expired' "
                "WHERE offer_id = ? AND status = 'pending'",
                (offer_id,),
            )
            row = conn.execute(
                "SELECT * FROM offers WHERE offer_id = ?", (offer_id,)
            ).fetchone()

        return _offer_to_dict(row)


def get_chain(chain_id: str) -> dict[str, Any]:
    with read_tx() as conn:
        chain = conn.execute(
            "SELECT * FROM chains WHERE chain_id = ?", (chain_id,)
        ).fetchone()
        if chain is None:
            raise ChainError(404, "not_found", f"链条不存在: {chain_id}")

        rows = conn.execute(
            "SELECT * FROM events WHERE chain_id = ? ORDER BY seq", (chain_id,)
        ).fetchall()

    return {
        "chain_id": chain["chain_id"],
        "file_hash": chain["file_hash"],
        "first_holder": chain["first_holder"],
        "tail_hash": chain["tail_hash"],
        "current_holder": chain["current_holder"],
        "length": len(rows),
        "created_at": chain["created_at"],
        "events": [_row_to_event(r) for r in rows],
    }


def list_chains() -> list[dict[str, Any]]:
    with read_tx() as conn:
        rows = conn.execute(
            """SELECT c.*, COUNT(e.seq) AS length
               FROM chains c
               LEFT JOIN events e ON e.chain_id = c.chain_id
               GROUP BY c.chain_id
               ORDER BY c.created_at, c.chain_id"""
        ).fetchall()
    return [
        {
            "chain_id": r["chain_id"],
            "file_hash": r["file_hash"],
            "current_holder": r["current_holder"],
            "tail_hash": r["tail_hash"],
            "length": r["length"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def verify_chain(chain_id: str) -> dict[str, Any]:
    """重放全链并指出首个断裂序号。

    对每个事件重新计算哈希并与存储值比对，同时校验序号连续与 prev_hash 指向。
    另将重算出的链尾与 chains 头信息比对，以发现头部被直接篡改的情况。
    """
    with read_tx() as conn:
        chain = conn.execute(
            "SELECT * FROM chains WHERE chain_id = ?", (chain_id,)
        ).fetchone()
        if chain is None:
            raise ChainError(404, "not_found", f"链条不存在: {chain_id}")
        rows = conn.execute(
            "SELECT * FROM events WHERE chain_id = ? ORDER BY seq", (chain_id,)
        ).fetchall()

    length = len(rows)
    expected_next_seq = length
    prev_hash = ""
    broken_seq: Optional[int] = None
    reason: Optional[str] = None

    for i, row in enumerate(rows):
        if row["seq"] != i:
            broken_seq, reason = row["seq"], f"序号不连续：期望 {i}，实际 {row['seq']}"
            break

        if row["seq"] == 0:
            payload = _genesis_payload(chain["file_hash"], chain["first_holder"])
        else:
            payload = _handover_payload(
                seq=row["seq"],
                prev_hash=row["prev_hash"],
                giver=row["giver"],
                receiver=row["receiver"],
                nonce=row["nonce"],
            )
        recomputed = digest_hex(canonical_json(payload))

        if recomputed != row["event_hash"]:
            broken_seq, reason = (
                row["seq"],
                f"事件哈希不匹配：记录 {row['event_hash']}，重算 {recomputed}"
                + ("（文件摘要或首位持有人被篡改）" if row["seq"] == 0 else ""),
            )
            break

        if row["event_hash"] != row["prev_hash"] and row["prev_hash"] != prev_hash:
            broken_seq, reason = (
                row["seq"],
                f"前序指针断裂：期望指向 {prev_hash}，实际指向 {row['prev_hash']}",
            )
            break

        prev_hash = row["event_hash"]

    recomputed_tail = prev_hash if broken_seq is None else None

    if broken_seq is None and recomputed_tail != chain["tail_hash"]:
        # 事件本身全部可验证，但链条头的链尾缓存被篡改：没有具体事件序号可指
        reason = (
            "链条头部信息被篡改：链尾哈希为 "
            f"{chain['tail_hash']}，按事件重算应为 {recomputed_tail}"
        )
        # first_broken_seq 保持 null
    valid = broken_seq is None and recomputed_tail == chain["tail_hash"]
    if valid and length > 0:
        # 最后一个事件的接收方必须等于头信息中的当前持有人
        last_holder = chain["first_holder"] if length == 1 else rows[-1]["receiver"]
        if chain["current_holder"] != last_holder:
            valid = False
            reason = (
                "链条头部信息被篡改：当前持有人为 "
                f"{chain['current_holder']}，按事件推导应为 {last_holder}"
            )

    return {
        "chain_id": chain_id,
        "valid": valid,
        "length": length,
        "expected_next_seq": expected_next_seq,
        "first_broken_seq": broken_seq,
        "reason": reason,
        "recomputed_tail_hash": recomputed_tail,
        "stored_tail_hash": chain["tail_hash"],
    }
