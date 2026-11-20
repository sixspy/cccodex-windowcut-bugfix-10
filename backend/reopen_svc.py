"""窗重开抬代：甲路径（抬代修复卡 2026-11-21 择定，当日禁止改口）。

重开同事务 generation+1 并作废旧物化；任一步失败，世代与作废整体回滚。
偏离修复卡（如乙路径「增量补丁」）拒写并记审计「抬代偏了」。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user
from db import connect, write_audit
from locks import release, try_acquire
from models import ReopenIn

router = APIRouter(tags=["reopen"])

CARD_MODE = "作废重算"  # 抬代修复卡择定甲路径；乙路径「增量补丁」当日禁改口


@router.get("/api/reopen-ledger")
def list_reopen(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT r.*, w.name AS window_name
               FROM reopen_ledger r
               JOIN windows w ON w.id=r.window_id
               ORDER BY r.id DESC"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


def _void_old_materializations(conn, wid: int) -> list[dict]:
    """作废旧物化：删除本窗全部物化行，返回明细留痕。

    对账只认本窗最新物化行；若仅标记「已作废」而留行，旧物化仍挂当前代、
    对账继续判齐，五处不同态。故作废以删除落库，明细写入审计载荷。
    """
    rows = conn.execute(
        """SELECT id, partition_key, row_count, total_amount
           FROM materializations WHERE window_id=? ORDER BY id""",
        (wid,),
    ).fetchall()
    conn.execute("DELETE FROM materializations WHERE window_id=?", (wid,))
    return [dict(r) for r in rows]


@router.post("/api/windows/{wid}/reopen-stub")
def reopen_stub(wid: int, body: ReopenIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    lock_key = f"reopen:{wid}"
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as conn:
        # 占重开锁：锁先落库他人可见；后到只记争用（落库）不写业务
        if not try_acquire(conn, lock_key, user):
            conn.commit()
            raise HTTPException(409, "争用：重开锁已被占，后到只记争用不写业务")
        conn.commit()
        try:
            w = conn.execute("SELECT * FROM windows WHERE id=?", (wid,)).fetchone()
            if not w:
                raise HTTPException(404, "窗口不存在")
            if w["status"] != "已关闭":
                raise HTTPException(409, "仅已关闭窗可登记重开抬代")
            if body.mode != CARD_MODE:
                write_audit(
                    conn,
                    user,
                    "抬代偏了",
                    str(wid),
                    {
                        "requested_mode": body.mode,
                        "card_mode": CARD_MODE,
                        "card": "甲路径",
                    },
                )
                conn.commit()
                raise HTTPException(409, "偏离抬代修复卡：当日择定甲路径（作废重算），拒写")
            from_gen = int(w["generation"])
            to_gen = from_gen + 1
            # 甲路径：同事务抬代 + 作废旧物化；失败则世代与作废整体回滚
            conn.execute(
                "UPDATE windows SET generation=? WHERE id=?", (to_gen, wid)
            )
            voided = _void_old_materializations(conn, wid)
            conn.execute(
                """INSERT INTO reopen_ledger(window_id,from_generation,to_generation,mode,status,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (wid, from_gen, to_gen, body.mode, "已抬代", now),
            )
            write_audit(
                conn,
                user,
                "reopen_bump",
                str(wid),
                {
                    "mode": body.mode,
                    "from_generation": from_gen,
                    "to_generation": to_gen,
                    "voided_materializations": voided,
                },
            )
            conn.commit()
            return {
                "ok": True,
                "stub": True,
                "mode": body.mode,
                "from_generation": from_gen,
                "to_generation": to_gen,
                "generation": to_gen,
                "voided_materializations": len(voided),
            }
        except Exception:
            conn.rollback()  # 世代与作废一起回滚
            raise
        finally:
            release(conn, lock_key, user)
            conn.commit()
