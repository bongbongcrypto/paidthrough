# -*- coding: utf-8 -*-
"""Telegram summary card for the keeper (stdlib only).

Self-contained on purpose (standard library only):
  * card() / plain() / problems(): build the summary card, strip it to plain text, lint its wording
  * send_card(): Telegram sendMessage as HTML, retried as plain text if Telegram refuses the HTML

Card layout: bold Korean title -> 1-3 plain Korean sentences -> "할 일: ..." -> #paidthrough.
Credentials: env file from $PAIDTHROUGH_ALERT_ENV (default ~/.alert.env): BOT_TOKEN_PAIDTHROUGH (+ optional
CHAT_ID_PAIDTHROUGH), else BOT_TOKEN + USER_DIRECT_CHAT_ID (or CHAT_ID). The token is never printed.
"""
from __future__ import annotations

import html as _html
import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path

TAG = "paidthrough"

# --- card format ---
MAX_BODY = 4
MAX_LINE = 80
JARGON = ("펀딩", "레짐", "패리티", "불변식", "판별자", "백테스트", "라이브", "전역사", "확증", "소스",
          "MDD", "PnL", "WS", "OI", "BT", "n=", "%/8h", "**", "__")
_WORD = re.compile(r"[A-Za-z0-9_]*[A-Za-z][A-Za-z0-9_]*")
_URL = re.compile(r"https?://\S+")
_HANGUL = re.compile(r"[가-힣]")
_LATIN = re.compile(r"[A-Za-z0-9]+")


def card(title: str, lines, todo: str = "없음", next_step: str = "", link: str = "", tag: str = "") -> str:
    body = [str(x) for x in (lines or []) if str(x).strip()]
    out = ["<b>" + _html.escape(title) + "</b>"] + [_html.escape(x) for x in body] + [""]
    out.append(_html.escape("할 일: " + (todo or "없음")))
    if next_step:
        out.append(_html.escape("다음: " + next_step))
    if link:
        out.append(_html.escape(link))
    if tag:
        out.append("#" + re.sub(r"[^0-9A-Za-z가-힣_]", "_", tag))
    return "\n".join(out)[:4000]


def plain(text: str) -> str:
    return _html.unescape(re.sub(r"</?[a-zA-Z][^>]*>", "", text))


def problems(text: str, allow=()) -> list:
    allow = {str(a) for a in allow}
    lines = plain(text).split("\n")
    out = []
    if not lines or not lines[0].strip():
        return ["제목이 비어 있음"]
    if not text.startswith("<b>"):
        out.append("제목이 굵지 않음")
    if not _HANGUL.search(lines[0]):
        out.append("제목에 한국어가 없음: " + lines[0][:40])
    todo = [i for i, x in enumerate(lines) if x.startswith("할 일: ")]
    if len(todo) != 1:
        out.append("'할 일:' 줄이 정확히 한 번 있어야 함 (지금 %d번)" % len(todo))
        body = lines[1:]
    else:
        body = [x for x in lines[1:todo[0]] if x.strip()]
    if len(body) > MAX_BODY:
        out.append("본문 %d줄 (최대 %d줄)" % (len(body), MAX_BODY))
    for x in lines:
        if x.startswith("#") or _URL.fullmatch(x.strip() or "-"):
            continue
        if len(x) > MAX_LINE:
            out.append("한 줄이 너무 김 (%d자): %s…" % (len(x), x[:30]))
        rest = _URL.sub("", x)
        for j in JARGON:
            hit = (re.search(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(j), rest) if _LATIN.fullmatch(j)
                   else j in rest)
            if hit and j not in allow:
                out.append("내부 용어 '%s': %s" % (j, x[:40]))
        for w in _WORD.findall(rest):
            if w not in allow:
                out.append("영어 낱말 '%s': %s" % (w, x[:40]))
    return out


# --- run summary -> card ---
NET_KO = {"mainnet": "메인넷", "testnet": "테스트넷"}
SKIP_KO = {
    "revert": "미리 돌려 보니 계약이 거절함 (낸 사람 주소가 차단됐을 수 있음)",
    "state_changed": "그새 상태가 바뀌어 환불 대상이 아님",
    "inflight": "앞서 보낸 환불 거래가 아직 확인 중",
    "no_gas_money": "키퍼 지갑에 가스비가 모자람",
    "fee_cap": "가스비가 상한보다 비쌈",
    "max_sends": "한 번에 보낼 건수 상한에 걸림",
}


def _ids(items) -> str:
    """'3, 7번' or '3, 7, 9, 12번 외 4건' (keeps a card line under 80 characters)."""
    ids = [str(x["id"]) for x in items]
    head = ", ".join(ids[:4]) + "번"
    return head + (" 외 %d건" % (len(ids) - 4) if len(ids) > 4 else "")


def summary_card(network: str, sent: list, skipped: list, failed: list, error: str = "") -> str:
    """sent: [{'id', 'amount' (6-dec units), 'fee_wei'}]; skipped: [{'id','reason'}]; failed: [{'id','why'}].
    Returns '' when there is nothing worth a message."""
    from fmt import usdc6, fee_usdc  # local import keeps this module importable alone

    net = NET_KO.get(network, network)
    if error:
        return card("%s 키퍼가 체인을 읽지 못함" % net,
                    ["이번 실행에서 체인 상태를 확인하지 못해 아무 거래도 보내지 않았습니다.",
                     "다음 실행 때 다시 시도합니다."],
                    todo="몇 시간 계속되면 서버 확인", tag=TAG)
    if not (sent or skipped or failed):
        return ""
    lines = []
    if sent:
        total = sum(s["amount"] for s in sent)
        fee = sum(s.get("fee_wei") or 0 for s in sent)
        lines.append("기한 안에 찾아가지 않은 청구서 %d건을 낸 사람에게 돌려보냈습니다." % len(sent))
        lines.append("합계 %s USDC, 가스비 %s USDC." % (usdc6(total), fee_usdc(fee)))
    if failed:
        lines.append("청구서 %s 환불 거래가 실패했거나 확인되지 않았습니다." % _ids(failed))
    if skipped:
        first = skipped[0]
        lines.append("청구서 %s 이번에 보류." % _ids(skipped))
        lines.append("사유: %s." % SKIP_KO.get(first["reason"], "사유 확인 필요"))
    if failed:
        title, todo = "%s 자동 환불 확인 필요" % net, "서버 기록에서 거래 결과 확인"
    elif any(s["reason"] == "no_gas_money" for s in skipped):
        title, todo = "%s 자동 환불 멈춤" % net, "키퍼 지갑에 가스비 USDC 충전"
    elif sent:
        title, todo = "%s 자동 환불 %d건 완료" % (net, len(sent)), "없음"
    else:
        title, todo = "%s 자동 환불 %d건 보류" % (net, len(skipped)), "없음, 다음 실행 때 다시 확인"
    return card(title, lines[:MAX_BODY], todo=todo, tag=TAG)


# --- sending ---
def _read_env(path: Path) -> dict:
    kv = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip().strip('"').strip("'")
    return kv


def alert_env_path() -> Path:
    return Path(os.environ.get("PAIDTHROUGH_ALERT_ENV") or Path.home() / ".alert.env").expanduser()


def send_card(html_text: str, env_path: Path = None, opener=None) -> tuple:
    """(ok, short reason). HTML first, the same words as plain text if Telegram refuses the HTML.
    Never raises; never returns or prints the token."""
    opener = opener or urllib.request.urlopen
    try:
        kv = _read_env(env_path or alert_env_path())
    except OSError:
        return False, "alert env file not found"
    tok = kv.get("BOT_TOKEN_PAIDTHROUGH") or kv.get("BOT_TOKEN")
    cid = (kv.get("CHAT_ID_PAIDTHROUGH") if kv.get("BOT_TOKEN_PAIDTHROUGH") else None) \
        or kv.get("USER_DIRECT_CHAT_ID") or kv.get("CHAT_ID")
    if not (tok and cid):
        return False, "alert env has no token or chat id"
    url = "https://api.telegram.org/bot%s/sendMessage" % tok
    last = "not sent"
    for payload in ({"chat_id": cid, "text": html_text[:4000], "parse_mode": "HTML",
                     "disable_web_page_preview": "true"},
                    {"chat_id": cid, "text": plain(html_text)[:4000]}):
        try:
            with opener(url, urllib.parse.urlencode(payload).encode("utf-8"), timeout=10) as r:
                body = json.loads(r.read().decode("utf-8", "replace"))
            if body.get("ok"):
                return True, "sent"
            last = "telegram refused"
        except Exception as e:  # noqa: BLE001 - alerts are best-effort; reason class only (URL holds the token)
            last = type(e).__name__
    return False, last
