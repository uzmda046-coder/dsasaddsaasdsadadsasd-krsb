import asyncio
import json
import math
import os
import subprocess
import sys
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

BASE_URL = "https://kick.com/api/v2"
DISCORD_API = "https://discord.com/api/v10"
CONFIG_PATH = Path(__file__).with_name("config.json")
REQUIREMENTS_PATH = Path(__file__).with_name("requirements.txt")
VENV_PATH = Path(__file__).with_name(".venv")


def log(message: str) -> None:
    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    print(f"[{now}] {message}", flush=True)


def venv_python() -> Path:
    if os.name == "nt":
        return VENV_PATH / "Scripts" / "python.exe"
    return VENV_PATH / "bin" / "python"


def running_inside_venv() -> bool:
    return Path(sys.executable).resolve() == venv_python().resolve()


def bootstrap_venv() -> None:
    if running_inside_venv():
        return
    python = venv_python()
    created = False
    if not python.exists():
        print("First run: creating .venv...", flush=True)
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV_PATH)])
        created = True
    if created:
        print("Installing dependencies...", flush=True)
        subprocess.check_call(
            [str(python), "-m", "pip", "install", "-r", str(REQUIREMENTS_PATH)]
        )
    else:
        check = subprocess.run(
            [str(python), "-c", "import httpx"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if check.returncode != 0:
            print("Dependencies are incomplete; installing them...", flush=True)
            subprocess.check_call(
                [str(python), "-m", "pip", "install", "-r", str(REQUIREMENTS_PATH)]
            )
    os.execv(str(python), [str(python), str(Path(__file__).resolve()), *sys.argv[1:]])


bootstrap_venv()

import httpx  # noqa: E402


class HttpError(Exception):
    def __init__(self, status_code: int, body: str, retry_after: float | None = None):
        self.status_code = status_code
        self.body = body
        self.retry_after = retry_after
        super().__init__(f"HTTP {status_code}: {body[:500]}")


class FatalAuthError(Exception):
    pass


class RetryableError(Exception):
    def __init__(self, message: str, delay: float | None = None):
        self.delay = delay
        super().__init__(message)


def _fail(msg: str) -> None:
    log(f"CONFIG ERROR: {msg}")
    raise SystemExit(1)


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        _fail(f"config.json not found at {CONFIG_PATH}")
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"cannot read config.json: {exc}")
    if not isinstance(raw, dict):
        _fail("config.json must be a JSON object")

    cfg: dict[str, Any] = {}

    token = raw.get("token")
    if not isinstance(token, str) or not token.strip():
        _fail("token must be a non-empty string")
    token = token.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    cfg["token"] = token

    channel = raw.get("channel_slug")
    if not isinstance(channel, str) or not channel.strip():
        _fail("channel_slug must be a non-empty string")
    cfg["channel_slug"] = channel.strip()

    message = raw.get("redeem_message")
    if not isinstance(message, str):
        _fail("redeem_message must be a string (may be empty)")
    cfg["redeem_message"] = message

    ts = raw.get("activation_timestamp")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        _fail("activation_timestamp must be a number (UNIX seconds, 0 = immediate)")
    cfg["activation_timestamp"] = float(ts)

    new_flag = raw.get("target_new_redeems")
    if not isinstance(new_flag, bool):
        _fail("target_new_redeems must be a boolean")
    paused_flag = raw.get("target_paused_redeems")
    if not isinstance(paused_flag, bool):
        _fail("target_paused_redeems must be a boolean")
    if not new_flag and not paused_flag:
        _fail("at least one of target_new_redeems / target_paused_redeems must be true")
    cfg["target_new_redeems"] = new_flag
    cfg["target_paused_redeems"] = paused_flag

    bot_token = raw.get("discord_bot_token")
    if not isinstance(bot_token, str):
        _fail("discord_bot_token must be a string (empty to disable Discord)")
    channel_id = raw.get("discord_channel_id")
    if not isinstance(channel_id, str):
        _fail("discord_channel_id must be a string (empty to disable Discord)")
    bot_token = bot_token.strip()
    channel_id = channel_id.strip()
    if (bot_token == "") != (channel_id == ""):
        _fail("discord_bot_token and discord_channel_id must be both empty or both non-empty")
    cfg["discord_bot_token"] = bot_token
    cfg["discord_channel_id"] = channel_id
    cfg["discord_enabled"] = bool(bot_token)

    dnd = raw.get("discord_notify_delay")
    if isinstance(dnd, bool) or not isinstance(dnd, (int, float)) or dnd < 0:
        _fail("discord_notify_delay must be a number >= 0")
    cfg["discord_notify_delay"] = float(dnd)

    sm = raw.get("slow_mod")
    if isinstance(sm, bool) or not isinstance(sm, (int, float)):
        _fail("slow_mod must be a number")
    sm = float(sm)
    if sm < 0:
        log(f"config.slow_mod={sm} is negative; using 0.0")
        sm = 0.0
    cfg["slow_mod"] = sm

    pi = raw.get("poll_interval")
    if isinstance(pi, bool) or not isinstance(pi, (int, float)) or pi <= 0:
        _fail("poll_interval must be a number > 0")
    cfg["poll_interval"] = float(pi)

    rt = raw.get("request_timeout")
    if isinstance(rt, bool) or not isinstance(rt, (int, float)) or rt <= 0:
        _fail("request_timeout must be a number > 0")
    cfg["request_timeout"] = float(rt)

    rd = raw.get("retry_delay")
    if isinstance(rd, bool) or not isinstance(rd, (int, float)) or rd < 0:
        _fail("retry_delay must be a number >= 0")
    cfg["retry_delay"] = float(rd)

    mra = raw.get("max_redeem_attempts")
    if isinstance(mra, bool) or not isinstance(mra, int) or mra < 1:
        _fail("max_redeem_attempts must be an integer >= 1")
    cfg["max_redeem_attempts"] = mra

    ua = raw.get("user_agent")
    if not isinstance(ua, str) or not ua.strip():
        _fail("user_agent must be a non-empty string")
    cfg["user_agent"] = ua.strip()

    return cfg


def eff_poll(cfg: dict[str, Any]) -> float:
    return cfg["poll_interval"] + cfg["slow_mod"]


def eff_retry(cfg: dict[str, Any]) -> float:
    return cfg["retry_delay"] + cfg["slow_mod"]


def eff_timeout(cfg: dict[str, Any]) -> float:
    return cfg["request_timeout"] + cfg["slow_mod"]


def request_headers(cfg: dict[str, Any]) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {cfg['token']}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://kick.com",
        "Referer": "https://kick.com/",
        "X-App-Platform": "web",
        "User-Agent": cfg["user_agent"],
    }


def retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_transaction_id() -> str:
    timestamp_ms = int(time.time() * 1000)
    random_80 = secrets.randbits(80)
    value = (timestamp_ms << 80) | random_80
    chars = []
    for _ in range(26):
        chars.append(_ULID_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    try:
        response = await client.request(method, url, json=json_body, params=params)
    except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
        raise RetryableError(f"network/timeout: {exc}") from exc
    if response.status_code in (401, 403):
        raise FatalAuthError(f"HTTP {response.status_code}: {response.text[:500]}")
    if response.status_code == 429:
        delay = retry_after_seconds(response)
        raise RetryableError("HTTP 429 rate limit", delay=delay)
    if 500 <= response.status_code <= 599:
        raise RetryableError(
            f"HTTP {response.status_code} server error: {response.text[:500]}"
        )
    if not 200 <= response.status_code <= 299:
        raise HttpError(
            response.status_code, response.text[:500], retry_after_seconds(response)
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise HttpError(response.status_code, "invalid JSON") from exc
    return data if isinstance(data, dict) else {"data": data}


def extract_data(payload: dict[str, Any]) -> Any:
    return payload.get("data", payload)


async def get_points(client: httpx.AsyncClient, channel: str) -> Any:
    return extract_data(
        await request_json(client, "GET", f"{BASE_URL}/channels/{channel}/points")
    )


async def get_rewards(client: httpx.AsyncClient, channel: str) -> list[dict[str, Any]]:
    payload = await request_json(client, "GET", f"{BASE_URL}/channels/{channel}/rewards")
    data = extract_data(payload)
    if isinstance(data, dict):
        data = data.get("rewards", data.get("data", []))
    if not isinstance(data, list):
        return []
    return [r for r in data if isinstance(r, dict)]


def points_value(points: Any) -> Any:
    if isinstance(points, dict):
        for key in ("points", "balance", "amount", "value"):
            if key in points:
                return points[key]
    return points


def numeric_points(points: Any) -> float | None:
    value = points_value(points)
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def collect_baseline(
    client: httpx.AsyncClient,
    channel: str,
    cfg: dict[str, Any],
    label: str,
) -> tuple[set[str], set[str]]:
    """One baseline snapshot. Returns (known_ids, paused_ids)."""
    while True:
        try:
            snapshot = await get_rewards(client, channel)
            break
        except Exception as exc:
            log(
                f"{label}: request failed ({type(exc).__name__}: {exc}); "
                f"retrying in {eff_retry(cfg):.2f}s"
            )
            await asyncio.sleep(eff_retry(cfg))

    known: set[str] = set()
    paused: set[str] = set()
    for reward in snapshot:
        rid = reward.get("id")
        if rid is None:
            continue
        rid = str(rid)
        known.add(rid)
        if bool(reward.get("is_paused")):
            paused.add(rid)
    return known, paused


async def redeem_once(
    client: httpx.AsyncClient,
    channel: str,
    reward: dict[str, Any],
    cfg: dict[str, Any],
) -> tuple[str, float | None]:
    """
    One redemption attempt (called under redeem_lock).
    Returns one of:
      ("success", None)
      ("insufficient", None)
      ("retry", override_delay_or_None)
    """
    reward_id = str(reward["id"])
    cost = numeric_points(reward.get("cost"))
    free_reward = cost is not None and cost <= 0

    try:
        baseline_raw = await get_points(client, channel)
    except FatalAuthError as exc:
        log(f"[{reward_id}] points read auth error: {exc}")
        return ("retry", None)
    except RetryableError as exc:
        log(f"[{reward_id}] points read retry: {exc}")
        return ("retry", exc.delay)
    except HttpError as exc:
        log(f"[{reward_id}] points read HTTP {exc.status_code}: {exc.body}")
        return ("retry", exc.retry_after)
    except Exception as exc:
        log(f"[{reward_id}] points read unexpected: {type(exc).__name__}: {exc}")
        return ("retry", None)

    baseline = numeric_points(baseline_raw)
    if baseline is None:
        log(f"[{reward_id}] cannot parse points: {baseline_raw!r}")
        return ("retry", None)

    if cost is not None and not free_reward and baseline < cost:
        log(f"[{reward_id}] insufficient points: balance={baseline:g}, cost={cost:g}")
        return ("insufficient", None)

    log(f"[{reward_id}] baseline points={baseline:g}")

    transaction_id = new_transaction_id()
    payload = {"transaction_id": transaction_id, "message": cfg["redeem_message"]}
    url = f"{BASE_URL}/channels/{channel}/rewards/{reward_id}/redeem"

    log(
        f"[{reward_id}] POST redeem | tx={transaction_id} | "
        f"msg_len={len(cfg['redeem_message'])}"
    )

    try:
        response = await client.post(url, json=payload)
    except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
        log(f"[{reward_id}] redeem network error: {exc}")
        return ("retry", None)

    body_preview = response.text[:1000]
    log(f"[{reward_id}] redeem response: HTTP {response.status_code} | body={body_preview}")

    if response.status_code in (401, 403):
        log(f"[{reward_id}] redeem auth error: HTTP {response.status_code}")
        return ("retry", None)
    if response.status_code == 429:
        return ("retry", retry_after_seconds(response))
    if 500 <= response.status_code <= 599:
        return ("retry", None)
    if response.status_code not in (200, 201, 202):
        return ("retry", retry_after_seconds(response))

    try:
        data = response.json()
    except ValueError:
        data = {"raw_response": body_preview}
    if not isinstance(data, dict):
        data = {"data": data}

    log(
        f"[{reward_id}] HTTP accepted | redemption_id={data.get('id')} | "
        f"tx={data.get('transaction_id')}"
    )

    if free_reward:
        log(f"[{reward_id}] free reward (cost={cost!r}); accepted as success")
        return ("success", None)

    try:
        current_raw = await get_points(client, channel)
    except FatalAuthError as exc:
        log(f"[{reward_id}] points verify auth error: {exc}")
        return ("retry", None)
    except RetryableError as exc:
        return ("retry", exc.delay)
    except HttpError as exc:
        return ("retry", exc.retry_after)
    except Exception as exc:
        log(f"[{reward_id}] points verify unexpected: {type(exc).__name__}: {exc}")
        return ("retry", None)

    current = numeric_points(current_raw)
    log(
        f"[{reward_id}] points verify: before={baseline:g} | "
        f"after={current if current is not None else current_raw}"
    )

    if current is not None and current < baseline:
        log(f"[{reward_id}] POINTS DECREASED: {baseline:g} -> {current:g}. SUCCESS")
        return ("success", None)

    log(f"[{reward_id}] points did not decrease.")
    return ("retry", None)


async def redemption_worker(
    client: httpx.AsyncClient,
    channel: str,
    reward: dict[str, Any],
    cfg: dict[str, Any],
    redeem_lock: asyncio.Lock,
    on_success: Callable[[dict[str, Any]], None],
    on_give_up: Callable[[dict[str, Any]], None],
) -> None:
    reward_id = str(reward["id"])
    max_attempts = cfg["max_redeem_attempts"]

    for attempt in range(1, max_attempts + 1):
        log(f"[{reward_id}] Attempt {attempt}/{max_attempts}")
        try:
            async with redeem_lock:
                kind, override = await redeem_once(client, channel, reward, cfg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log(f"[{reward_id}] UNEXPECTED WORKER ERROR: {type(exc).__name__}: {exc}")
            kind, override = ("retry", None)

        if kind == "success":
            on_success(reward)
            return
        if kind == "insufficient":
            on_give_up(reward)
            return

        sleep_for = override if override is not None else eff_retry(cfg)
        await asyncio.sleep(sleep_for)

    log(f"[{reward_id}] GAVE UP after {max_attempts} attempts.")
    on_give_up(reward)


async def send_discord_notify(
    cfg: dict[str, Any],
    title: str,
    cost: Any,
    reward_id: str,
) -> None:
    await asyncio.sleep(cfg["discord_notify_delay"])
    content = (
        f'✅ Redeemed "{title}" | cost: {cost} | reward_id: {reward_id} | '
        f"at {datetime.now(timezone.utc).isoformat(timespec='seconds')}"
    )
    url = f"{DISCORD_API}/channels/{cfg['discord_channel_id']}/messages"
    headers = {
        "Authorization": f"Bot {cfg['discord_bot_token']}",
        "Content-Type": "application/json",
        "User-Agent": cfg["user_agent"],
    }
    try:
        async with httpx.AsyncClient(timeout=eff_timeout(cfg)) as dc:
            resp = await dc.post(url, json={"content": content}, headers=headers)
        if resp.status_code >= 400:
            log(
                f"[discord] notify failed for {reward_id}: "
                f"HTTP {resp.status_code}: {resp.text[:300]}"
            )
        else:
            log(f"[discord] notify sent for {reward_id}")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log(f"[discord] notify error for {reward_id}: {type(exc).__name__}: {exc}")


async def wait_for_activation(
    client: httpx.AsyncClient,
    cfg: dict[str, Any],
    known_ids: set[str],
) -> None:
    """Handles baseline #2 (if needed) and waits until activation time."""
    now_ts = time.time()
    act_ts = cfg["activation_timestamp"]

    if act_ts <= 0 or math.isnan(act_ts) or math.isinf(act_ts) or act_ts <= now_ts:
        log(
            f"ACTIVATION: immediate "
            f"(timestamp={act_ts}, now={now_ts:.0f})"
        )
        return

    wait_total = act_ts - now_ts
    log(
        f"ACTIVATION scheduled in {wait_total:.1f}s "
        f"at {datetime.fromtimestamp(act_ts).isoformat()}"
    )

    if wait_total >= 60:
        pre_wait = wait_total - 60
        log(f"Waiting {pre_wait:.1f}s until baseline #2 window...")
        await asyncio.sleep(pre_wait)

        log("BASELINE #2 (60s before activation)...")
        known_2, _paused_2 = await collect_baseline(
            client, cfg["channel_slug"], cfg, "BASELINE #2"
        )
        discovered = known_2 - known_ids
        known_ids |= known_2
        log(
            f"BASELINE #2: {len(known_2)} known id(s), "
            f"{len(discovered)} newly discovered since baseline #1"
        )
        for rid in sorted(discovered):
            log(f"  NEW-SINCE-B1: {rid}")

        log("Waiting 60s until activation...")
        await asyncio.sleep(60)
    else:
        log(f"Waiting {wait_total:.1f}s until activation...")
        await asyncio.sleep(wait_total)

    log("ACTIVATED")


async def main() -> None:
    cfg = load_config()
    log("Config loaded and validated.")
    log(f"Channel: {cfg['channel_slug']}")
    log(
        f"target_new_redeems={cfg['target_new_redeems']} | "
        f"target_paused_redeems={cfg['target_paused_redeems']}"
    )
    log(f"slow_mod={cfg['slow_mod']}s | discord={'on' if cfg['discord_enabled'] else 'off'}")

    timeout = httpx.Timeout(eff_timeout(cfg))

    async with httpx.AsyncClient(headers=request_headers(cfg), timeout=timeout) as client:
        log("Checking authorization via GET /points...")
        while True:
            try:
                points = await get_points(client, cfg["channel_slug"])
                break
            except Exception as exc:
                log(
                    f"Auth check failed ({type(exc).__name__}: {exc}); "
                    f"retrying in {eff_retry(cfg):.2f}s"
                )
                await asyncio.sleep(eff_retry(cfg))
        log(f"Auth OK. Points: {points_value(points)}")

        log("BASELINE #1 (at startup)...")
        known_ids, baseline_paused_ids = await collect_baseline(
            client, cfg["channel_slug"], cfg, "BASELINE #1"
        )
        log(
            f"BASELINE #1: {len(known_ids)} known id(s), "
            f"{len(baseline_paused_ids)} paused"
        )
        for rid in sorted(known_ids):
            marker = " [PAUSED]" if rid in baseline_paused_ids else ""
            log(f"  KNOWN: {rid}{marker}")

        await wait_for_activation(client, cfg, known_ids)

        active_tasks: dict[str, asyncio.Task] = {}
        completed_ids: set[str] = set()
        given_up_ids: set[str] = set()
        skipped_no_input: set[str] = set()
        new_pending_paused: set[str] = set()
        redeem_lock = asyncio.Lock()

        def schedule_discord_notify(reward: dict[str, Any]) -> None:
            if not cfg["discord_enabled"]:
                return
            rid = str(reward["id"])
            title = reward.get("title", "<untitled>")
            cost = reward.get("cost")
            log(
                f"[discord] scheduled notify for {rid} in "
                f"{cfg['discord_notify_delay']}s"
            )
            asyncio.create_task(
                send_discord_notify(cfg, title, cost, rid),
                name=f"discord-{rid}",
            )

        def on_success(reward: dict[str, Any]) -> None:
            rid = str(reward["id"])
            completed_ids.add(rid)
            new_pending_paused.discard(rid)
            baseline_paused_ids.discard(rid)
            log(f"REWARD COMPLETE [{rid}]")
            schedule_discord_notify(reward)

        def on_give_up(reward: dict[str, Any]) -> None:
            rid = str(reward["id"])
            given_up_ids.add(rid)
            new_pending_paused.discard(rid)
            baseline_paused_ids.discard(rid)
            log(f"REWARD GAVE UP [{rid}]")

        def spawn_worker(reward: dict[str, Any]) -> None:
            rid = str(reward["id"])
            log(
                f"[{rid}] Starting redemption worker for "
                f"\"{reward.get('title', '')}\""
            )
            task = asyncio.create_task(
                redemption_worker(
                    client,
                    cfg["channel_slug"],
                    reward,
                    cfg,
                    redeem_lock,
                    on_success,
                    on_give_up,
                ),
                name=f"redeem-{rid}",
            )
            active_tasks[rid] = task

        log(
            f"Active polling started. "
            f"poll_interval_effective={eff_poll(cfg):.2f}s"
        )

        try:
            while True:
                t0 = asyncio.get_running_loop().time()

                for rid in list(active_tasks.keys()):
                    if active_tasks[rid].done():
                        del active_tasks[rid]

                try:
                    current = await get_rewards(client, cfg["channel_slug"])
                except Exception as exc:
                    log(
                        f"Polling error ({type(exc).__name__}: {exc}); "
                        f"retrying in {eff_retry(cfg):.2f}s"
                    )
                    await asyncio.sleep(eff_retry(cfg))
                    continue

                for reward in current:
                    rid_raw = reward.get("id")
                    if rid_raw is None:
                        continue
                    rid = str(rid_raw)

                    if (
                        rid in completed_ids
                        or rid in given_up_ids
                        or rid in skipped_no_input
                    ):
                        continue
                    existing = active_tasks.get(rid)
                    if existing is not None and not existing.done():
                        continue

                    paused = bool(reward.get("is_paused"))
                    enabled = bool(reward.get("is_enabled", True))
                    requires_input = bool(reward.get("is_user_input_required"))

                    is_new = rid not in known_ids

                    should_spawn = False

                    if is_new:
                        known_ids.add(rid)

                        if requires_input and not cfg["redeem_message"]:
                            log(
                                f"[{rid}] NEW: user input required but "
                                f"redeem_message is empty; skipping permanently"
                            )
                            skipped_no_input.add(rid)
                            continue

                        if cfg["target_new_redeems"]:
                            if paused:
                                new_pending_paused.add(rid)
                                log(
                                    f"[{rid}] NEW (paused): "
                                    f"{reward.get('title', '?')}; awaiting unpause"
                                )
                                continue
                            if enabled:
                                should_spawn = True
                            else:
                                log(
                                    f"[{rid}] NEW (disabled): "
                                    f"{reward.get('title', '?')}; not redeemable"
                                )
                    else:
                        if (
                            cfg["target_new_redeems"]
                            and rid in new_pending_paused
                        ):
                            if not paused and enabled:
                                if requires_input and not cfg["redeem_message"]:
                                    log(
                                        f"[{rid}] user input required but "
                                        f"redeem_message is empty; skipping permanently"
                                    )
                                    skipped_no_input.add(rid)
                                    new_pending_paused.discard(rid)
                                    continue
                                log(
                                    f"[{rid}] NEW(prev-paused) UNPAUSED: "
                                    f"{reward.get('title', '?')}"
                                )
                                new_pending_paused.discard(rid)
                                should_spawn = True
                            continue

                        if (
                            cfg["target_paused_redeems"]
                            and rid in baseline_paused_ids
                        ):
                            if not paused and enabled:
                                if requires_input and not cfg["redeem_message"]:
                                    log(
                                        f"[{rid}] user input required but "
                                        f"redeem_message is empty; skipping permanently"
                                    )
                                    skipped_no_input.add(rid)
                                    baseline_paused_ids.discard(rid)
                                    continue
                                log(
                                    f"[{rid}] BASELINE-PAUSED UNPAUSED: "
                                    f"{reward.get('title', '?')}"
                                )
                                baseline_paused_ids.discard(rid)
                                should_spawn = True

                    if should_spawn:
                        spawn_worker(reward)

                elapsed = asyncio.get_running_loop().time() - t0
                await asyncio.sleep(max(0.0, eff_poll(cfg) - elapsed))
        finally:
            if active_tasks:
                log(f"Stopping {len(active_tasks)} active worker(s)...")
                for task in active_tasks.values():
                    if not task.done():
                        task.cancel()


async def supervisor() -> None:
    while True:
        try:
            await main()
        except KeyboardInterrupt:
            raise
        except SystemExit:
            raise
        except Exception as exc:
            log(
                f"MAIN ERROR: {type(exc).__name__}: {exc}. "
                "Restarting in 3 seconds (Ctrl+C to stop)"
            )
            await asyncio.sleep(3)


if __name__ == "__main__":
    try:
        asyncio.run(supervisor())
    except KeyboardInterrupt:
        print("\nStopped by user.")