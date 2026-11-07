"""mall4j 后台的登录适配器（当前被测项目）。

**为什么单独写这个文件**

这个系统的登录链路有三步，框架内置的"滑块登录"套不上：

1. ``POST /_admin/captcha/get``  —— 取拼图验证码（返回背景图 + 拼图块 + 一把密钥）
2. ``POST /_admin/captcha/check`` —— 提交缺口横坐标做校验
3. ``POST /_admin/adminLogin``   —— 带验证码票据 + 加密密码登录

两个关键加密（都是前端 JS 里抄出来的）：

- **验证码坐标**：把 ``{"x": 缺口横坐标, "y": 5}`` 用密钥做 AES-ECB 加密，
  密钥是 ``captcha/get`` 返回的 ``secretKey``。
  ⚠️ ``y`` 固定传 5 —— 前端写死的，服务端也只比 x。
- **密码**：把 ``毫秒时间戳 + 明文密码`` 用固定密钥 ``-mall4j-password``
  做 AES-ECB 加密。

验证码成功后，前端还会把 ``token---{"x":..,"y":5}`` 再加密一次，
当成 ``captchaVerification`` 传给登录接口。

**怎么找缺口的横坐标**

拼图块图（``jigsawImageBase64``）带 alpha 通道，alpha 的形状就是缺口轮廓；
它是从背景图上抠下来的那块，所以拿它跟背景图做**像素相关**（Pearson）
就能定位到原位置。实测 20 次命中 19 次。

配置里这样指过来::

    login:
      type: custom
      custom_method: "adapters.mall4j.login"
"""
from __future__ import annotations

import base64
import json
import time
import uuid as _uuid
from typing import Any

import requests

from utils.logs.log_manager import log
from utils.config.project_profile import ProjectRegistry

#: 密码加密用的固定密钥（前端 JS 里的常量）
PASSWORD_KEY = "-mall4j-password"

#: 验证码坐标的 y 坐标 —— 前端写死，服务端也只比 x
CAPTCHA_FIXED_Y = 5

#: 验证码类型：2 = 拼图滑块（前端在 switch 里把 blockPuzzle 映射成 "2"）
CAPTCHA_TYPE = "2"


# ---------------------------------------------------------------------------
# AES-ECB 加密（前端用 crypto-js 的 AES.encrypt + Pkcs7 padding）
# ---------------------------------------------------------------------------

def _aes_ecb_base64(plain_text: str, key: str) -> str:
    """AES-ECB + Pkcs7 填充，输出 base64（和前端 crypto-js 结果一致）。"""
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad

    cipher = AES.new(key.encode("utf-8"), AES.MODE_ECB)
    encrypted = cipher.encrypt(pad(plain_text.encode("utf-8"), 16))
    return base64.b64encode(encrypted).decode("ascii")


def encrypt_password(password: str) -> str:
    """加密密码：``毫秒时间戳 + 明文密码`` → AES → base64。"""
    timestamp = str(int(time.time() * 1000))
    return _aes_ecb_base64(timestamp + password, PASSWORD_KEY)


# ---------------------------------------------------------------------------
# 验证码
# ---------------------------------------------------------------------------

def _fetch_captcha(session: requests.Session, api_base: str) -> dict[str, Any]:
    """取一道新的拼图验证码。

    返回服务端给的 ``repData``（含背景图、拼图块、密钥、token）。
    """
    resp = session.post(
        f"{api_base}/captcha/get",
        json={
            "captchaType": CAPTCHA_TYPE,
            "clientUid": f"slider-{_uuid.uuid4()}",
            "ts": int(time.time() * 1000),
        },
        timeout=20,
    )
    body = resp.json()
    data = body.get("data") or {}
    if data.get("repCode") != "0000":
        raise RuntimeError(f"取验证码失败：{body}")
    rep_data = data.get("repData") or {}
    if not rep_data.get("originalImageBase64") or not rep_data.get("jigsawImageBase64"):
        raise RuntimeError(f"验证码响应缺图片：{sorted(rep_data)}")
    return rep_data


def _locate_gap_x(original: bytes, jigsaw: bytes) -> int:
    """在背景图上找出拼图块原位置的横坐标。

    做法：拼图块的 alpha 通道就是缺口形状，把它当掩码，
    在背景图上逐列滑，用 Pearson 相关系数找最像的那一列。

    不用模板匹配（``matchTemplate``）是因为拼图块本身就是从背景抠下来的，
    直接匹配容易被背景里相似的纹理带偏；相关法更稳。
    """
    import cv2
    import numpy as np

    bg = cv2.imdecode(np.frombuffer(original, np.uint8), cv2.IMREAD_UNCHANGED)
    piece = cv2.imdecode(np.frombuffer(jigsaw, np.uint8), cv2.IMREAD_UNCHANGED)
    if bg is None or piece is None:
        raise RuntimeError("验证码图片解码失败")

    if bg.ndim == 3 and bg.shape[2] == 4:
        bg = bg[:, :, :3]
    if piece.ndim != 3 or piece.shape[2] != 4:
        raise RuntimeError(f"拼图块图格式不对：{piece.shape}")

    alpha = piece[:, :, 3]
    ys, xs = np.nonzero(alpha > 10)
    if len(ys) == 0:
        raise RuntimeError("拼图块 alpha 通道是空的")
    py0, py1 = int(ys.min()), int(ys.max())
    px0, px1 = int(xs.min()), int(xs.max())

    mask = alpha[py0:py1 + 1, px0:px1 + 1] > 10
    piece_rgb = piece[py0:py1 + 1, px0:px1 + 1, :3].astype(np.float64)
    bg_rgb = bg[:, :, :3].astype(np.float64)
    ph, pw = mask.shape

    best_score = -2.0
    best_x = 0
    for x in range(bg_rgb.shape[1] - pw + 1):
        window = bg_rgb[py0:py0 + ph, x:x + pw]
        a = window[mask].ravel()
        b = piece_rgb[mask].ravel()
        a = a - a.mean()
        b = b - b.mean()
        denom = np.linalg.norm(a) * np.linalg.norm(b)
        if denom == 0:
            continue
        score = float(a.dot(b) / denom)
        if score > best_score:
            best_score = score
            best_x = x

    log.info("缺口定位：x=%s（相关系数 %.3f）", best_x, best_score)
    return best_x


def _solve_captcha(session: requests.Session, api_base: str, retries: int = 3) -> tuple[str, int]:
    """过掉拼图滑块，返回 ``(验证码票据 captchaVerification, 缺口横坐标)``。

    验证码是一次性的 —— 提交过就作废，所以识别失败要**重新取题**再试。
    """
    last_error = "未知错误"
    for attempt in range(1, retries + 1):
        captcha = _fetch_captcha(session, api_base)
        x = _locate_gap_x(
            base64.b64decode(captcha["originalImageBase64"]),
            base64.b64decode(captcha["jigsawImageBase64"]),
        )
        point_json = _aes_ecb_base64(
            json.dumps({"x": int(x), "y": CAPTCHA_FIXED_Y}, separators=(",", ":")),
            captcha["secretKey"],
        )
        resp = session.post(
            f"{api_base}/captcha/check",
            json={
                "captchaType": CAPTCHA_TYPE,
                "pointJson": point_json,
                "token": captcha["token"],
            },
            timeout=20,
        )
        data = (resp.json().get("data") or {})
        if data.get("repCode") == "0000":
            # 前端成功时把 token 和坐标拼起来再加密一次，当成登录票据
            plain = f"{captcha['token']}---" + json.dumps(
                {"x": int(x), "y": CAPTCHA_FIXED_Y}, separators=(",", ":")
            )
            verification = _aes_ecb_base64(plain, captcha["secretKey"])
            log.info("验证码通过（第 %s 次尝试，x=%s）", attempt, x)
            return verification, x

        last_error = f"{data.get('repCode')} {data.get('repMsg')}"
        log.warning("验证码没通过（第 %s 次尝试，x=%s）：%s", attempt, x, last_error)

    raise RuntimeError(f"验证码连续 {retries} 次没过：{last_error}")


# ---------------------------------------------------------------------------
# 对外入口：框架的 custom 登录函数
# ---------------------------------------------------------------------------

def login(project_name: str, login_config: dict[str, Any], env: str) -> dict[str, Any]:
    """登录 mall4j 后台，返回 ``{"token": ..., "headers": {...}}``。

    Args:
        project_name: 项目名（配置里的 key）
        login_config: 配置里的 ``login`` 段
        env: 环境名

    Returns:
        含 ``token`` 的字典 —— 框架会把它写进全局变量，
        YAML 用例里就能写 ``{{mall4j.token}}``。
    """
    profile = ProjectRegistry.require(project_name, env)
    data = dict((login_config or {}).get("data") or {})

    username = data.get("username") or profile.username
    password = data.get("password") or profile.password
    if not username or not password:
        raise ValueError(
            f"项目 {project_name!r} 没配账号密码。"
            "把账号密码写进 .env（MALL4J_USERNAME / MALL4J_PASSWORD）"
        )

    host = profile.host.rstrip("/")
    api_base = host if host.endswith("/_admin") else f"{host}/_admin"

    log.info("[%s] 开始登录（用户名=%s，接口=%s）", project_name, username, api_base)

    session = requests.Session()
    # 不走系统代理（VPN / 抓包工具装的本机代理会拦请求，返回 502）
    session.trust_env = False
    session.headers.update({
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json; charset=utf-8",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": profile.effective_web_url or host,
        "Referer": (profile.effective_web_url or host) + "/",
    })

    verification, _x = _solve_captcha(session, api_base)

    resp = session.post(
        f"{api_base}/adminLogin",
        json={
            "userName": username,
            "passWord": encrypt_password(password),
            "captchaVerification": verification,
        },
        timeout=20,
    )
    body = resp.json()
    if not body.get("success"):
        raise RuntimeError(
            f"登录失败：code={body.get('code')} msg={body.get('msg')}"
        )

    login_data = body.get("data") or {}
    token = str(login_data.get("accessToken") or "")
    if not token:
        raise RuntimeError(f"登录响应里没有 accessToken：{body}")
    # refreshToken 用来调 /token/refresh 续期。普通业务用例用不到，
    # 但「刷新访问令牌」这类用例需要把它当请求参数，所以一并返回。
    refresh_token = str(login_data.get("refreshToken") or "")

    log.info("[%s] 登录成功，token 前缀=%s...", project_name, token[:12])

    # 这个系统用标准的 Authorization 头带 token，没有额外的会话头
    return {
        "token": token,
        "refresh_token": refresh_token,
        "uuid": "",
        "headers": profile.auth_headers(token, ""),
    }
