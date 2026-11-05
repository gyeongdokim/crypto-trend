"""
업비트 거래 API 클라이언트 (잔고 조회·주문·주문 조회)

인증 방식 (업비트 공식 규칙)
  1. 요청마다 JWT 토큰을 만든다: {access_key, nonce(매번 새 난수), query_hash}
  2. query_hash = 주문 파라미터를 "a=1&b=2" 문자열로 만든 뒤 SHA512 해시
     → 서버는 '토큰이 이 주문 내용에 대해 발급된 것인지' 확인 (중간에 내용 바꿔치기 방지)
  3. 토큰을 secret_key로 HMAC-SHA256 서명 → 헤더 "Authorization: Bearer <토큰>"
외부 라이브러리(PyJWT) 없이 표준 라이브러리로 직접 구현 → 원리가 그대로 보이게
"""
import base64
import hashlib
import hmac
import json
import math
import time
import uuid

import requests

BASE = "https://api.upbit.com"

# 업비트 오류 코드 → 사람이 읽는 설명 (텔레그램 알림용)
ERROR_HINT = {
    "no_authorization_ip": "허용되지 않은 IP — 집 공인 IP가 바뀌었을 가능성. 업비트 > Open API 관리에서 IP 수정",
    "invalid_access_key": "API 키가 틀렸거나 만료됨 (.env 확인, 키 유효기간 1년)",
    "jwt_verification": "인증 토큰 검증 실패 (secret 키 오타 또는 PC 시계 오차)",
    "expired_access_key": "API 키 만료 — 재발급 필요",
    "out_of_scope": "키 권한 부족 (자산조회·주문조회·주문하기 권한 확인)",
    "insufficient_funds_bid": "매수할 원화 잔고 부족",
    "insufficient_funds_ask": "매도할 코인 잔고 부족",
    "under_min_total_bid": "최소 주문금액(5,000원) 미만",
    "under_min_total_ask": "최소 주문금액(5,000원) 미만",
    "market_offline": "점검 등으로 해당 마켓 주문 불가",
    "duplicate_identifier": "같은 identifier 주문이 이미 있음 → 중복 주문 차단됨",
}


class UpbitError(Exception):
    def __init__(self, status, name, message):
        self.status, self.name, self.message = status, name, message
        hint = ERROR_HINT.get(name, "")
        super().__init__(f"[{status}] {name}: {message}" + (f"\n→ {hint}" if hint else ""))


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def make_jwt(access, secret, params=None):
    payload = {"access_key": access, "nonce": str(uuid.uuid4())}
    if params:
        q = "&".join(f"{k}={v}" for k, v in params.items())   # 인코딩 없이 그대로 (값은 영문·숫자·-.만 사용)
        payload["query_hash"] = hashlib.sha512(q.encode()).hexdigest()
        payload["query_hash_alg"] = "SHA512"
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{_b64(sig)}"


class Upbit:
    def __init__(self, access, secret):
        if not access or not secret:
            raise ValueError(".env에 UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY가 없음")
        self.access, self.secret = access, secret

    def _req(self, method, path, params=None):
        hdr = {"Authorization": f"Bearer {make_jwt(self.access, self.secret, params)}"}
        for attempt in range(3):
            if method == "GET":
                r = requests.get(BASE + path, params=params, headers=hdr, timeout=10)
            else:
                r = requests.post(BASE + path, json=params, headers=hdr, timeout=10)
            if r.status_code == 429:                 # 요청 수 제한 → 잠깐 쉬고 재시도
                time.sleep(0.5 * (attempt + 1))
                hdr = {"Authorization": f"Bearer {make_jwt(self.access, self.secret, params)}"}
                continue
            break
        if r.status_code >= 400:
            try:
                e = r.json().get("error", {})
            except ValueError:
                e = {}
            raise UpbitError(r.status_code, e.get("name", "unknown"), e.get("message", r.text[:200]))
        return r.json()

    # ── 조회 ──
    def balances(self):
        """{'KRW': 사용가능 원화, 'BTC': 사용가능 수량, ...} (주문 대기로 묶인 금액 제외)"""
        return {a["currency"]: float(a["balance"]) for a in self._req("GET", "/v1/accounts")}

    def get_order(self, uuid_):
        return self._req("GET", "/v1/order", {"uuid": uuid_})

    # ── 주문 ──
    @staticmethod
    def order_params(market, side, krw=None, volume=None, identifier=None):
        """시장가 매수 = 금액 지정(ord_type=price) / 시장가 매도 = 수량 지정(ord_type=market)"""
        if side == "bid":
            p = {"market": market, "side": "bid", "price": str(int(krw)), "ord_type": "price"}
        else:
            p = {"market": market, "side": "ask", "volume": f"{math.floor(volume * 1e8) / 1e8:.8f}", "ord_type": "market"}
        if identifier:
            p["identifier"] = identifier
        return p

    def place(self, params):
        return self._req("POST", "/v1/orders", params)

    def test_place(self, params):
        """주문 생성 테스트: 형식·권한·잔고만 검사하고 실제 주문은 안 냄"""
        return self._req("POST", "/v1/orders/test", params)

    def wait_fill(self, uuid_, timeout=15):
        """체결 완료까지 대기 → (체결수량, 체결대금, 수수료). 시장가 매수는 잔돈이 남아 state=cancel로 끝나는 게 정상"""
        t0 = time.time()
        while True:
            o = self.get_order(uuid_)
            if o["state"] in ("done", "cancel") or time.time() - t0 > timeout:
                break
            time.sleep(0.5)
        trades = o.get("trades") or []
        vol = sum(float(t["volume"]) for t in trades) or float(o.get("executed_volume") or 0)
        funds = sum(float(t["funds"]) for t in trades) or float(o.get("executed_funds") or 0)
        return vol, funds, float(o.get("paid_fee") or 0), o["state"]
