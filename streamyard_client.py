"""
StreamYard API client — reverse-engineered internal API.
Handles email OTP auth, broadcast listing, and video download.
"""
import logging
import pickle
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://streamyard.com"
SESSION_FILE = Path(__file__).parent / "session.pkl"


class StreamYardClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.7444.265 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        })
        self.csrf_token: str | None = None
        self._pending_email: str | None = None
        self._workspace_id: str | None = None
        self._load_session()

    # ------------------------------------------------------------------
    # Session persistence
    # ------------------------------------------------------------------

    def _load_session(self) -> None:
        if SESSION_FILE.exists():
            try:
                with open(SESSION_FILE, "rb") as f:
                    data = pickle.load(f)
                cookies = data.get("cookies", {})
                self.session.cookies.update(cookies)
                self.csrf_token = cookies.get("csrfToken")
            except Exception:
                SESSION_FILE.unlink(missing_ok=True)

    def _cookie(self, name: str, default=None):
        """Read a cookie value safely — handles StreamYard's duplicate cookie names."""
        values = [c.value for c in self.session.cookies if c.name == name]
        return values[-1] if values else default

    def _save_session(self) -> None:
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Deduplicate by keeping the last value for each cookie name
        cookies = {c.name: c.value for c in self.session.cookies}
        with open(SESSION_FILE, "wb") as f:
            pickle.dump({"cookies": cookies}, f)

    def clear_session(self) -> None:
        self.session.cookies.clear()
        self.csrf_token = None
        SESSION_FILE.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _refresh_csrf(self) -> str:
        """Fetch the login page to get a fresh csrfToken cookie."""
        # Drop any stale cookies from a previous session first — a leftover
        # expired jwt/csrfToken pair merged with the fresh ones below causes
        # StreamYard to send duplicate jwt cookies and reject with 401.
        self.session.cookies.clear()
        resp = self.session.get(f"{BASE_URL}/login", timeout=15)
        try:
            with open(Path(__file__).parent / 'service.log', 'a', encoding='utf-8') as f:
                f.write(f"[streamyard] refresh_csrf status={resp.status_code}\n")
        except Exception:
            pass
        resp.raise_for_status()
        token = self._cookie("csrfToken")
        if not token:
            raise RuntimeError("Could not retrieve CSRF token from StreamYard")
        self.csrf_token = token
        try:
            with open(Path(__file__).parent / 'service.log', 'a', encoding='utf-8') as f:
                f.write(f"[streamyard] refresh_csrf token={token}\n")
        except Exception:
            pass
        return token

    def request_otp(self, email: str) -> None:
        """Send an OTP code to the given email address."""
        csrf = self._refresh_csrf()
        headers = {
            "Referer": f"{BASE_URL}/login",
            "Origin": BASE_URL,
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }
        try:
            with open(Path(__file__).parent / 'service.log', 'a', encoding='utf-8') as f:
                f.write(f"[streamyard] request_otp email={email} csrf={csrf}\n")
        except Exception:
            pass
        resp = self.session.post(
            f"{BASE_URL}/api/user/login",
            json={"email": email, "csrfToken": csrf},
            headers=headers,
            timeout=15,
        )
        body_text = resp.text or ""
        try:
            with open(Path(__file__).parent / 'service.log', 'a', encoding='utf-8') as f:
                f.write(f"[streamyard] request_otp status={resp.status_code}\n")
                f.write(f"[streamyard] request_otp body={body_text[:2000]}\n")
        except Exception:
            pass
        logger.info(f"[streamyard] request_otp status={resp.status_code}")
        logger.info(f"[streamyard] request_otp body={body_text[:1000]}")
        if not resp.ok:
            try:
                data = resp.json()
                msg = data.get("error") or data.get("message") or body_text
            except Exception:
                msg = body_text or f"HTTP {resp.status_code}"
            raise RuntimeError(f"StreamYard error {resp.status_code}: {msg}")
        self._pending_email = email

    def verify_otp(self, otp: str) -> tuple[bool, str]:
        """
        Verify the OTP.
        Returns (True, "") on success, (False, error_message) on failure.
        """
        if not self._pending_email:
            return False, "No pending email — please enter your email again."

        resp = self.session.post(
            f"{BASE_URL}/api/user/otp_token",
            json={
                "email": self._pending_email,
                "csrfToken": self.csrf_token,
                "otpToken": otp.strip(),
            },
            headers={"Referer": f"{BASE_URL}/login"},
            timeout=15,
        )

        logger.info(f"[streamyard] verify_otp status={resp.status_code}")
        logger.info(f"[streamyard] verify_otp body={resp.text[:500]}")
        logger.info(f"[streamyard] cookies after verify={[(c.name, c.value[:20]) for c in self.session.cookies]}")

        if resp.ok:
            try:
                body = resp.json()
                # StreamYard may return {"error": "..."} with a 200 status
                if isinstance(body, dict) and body.get("error"):
                    return False, str(body["error"])
            except Exception:
                pass
            # Success — update csrf token from cookies and persist session
            self.csrf_token = self._cookie("csrfToken") or self.csrf_token
            self._save_session()
            return True, ""

        # Non-2xx — try to extract an error message from the body
        try:
            body = resp.json()
            msg = body.get("error") or body.get("message") or body
        except Exception:
            msg = resp.text or f"StreamYard returned {resp.status_code}"

        return False, f"{msg} (HTTP {resp.status_code})"

    def _get_team_id(self) -> str | None:
        """Fetch the user's team/organisation ID from the StreamYard API."""
        # Try a dedicated teams endpoint first
        for endpoint in ("/api/teams", "/api/user/teams", "/api/organizations"):
            try:
                resp = self.session.get(f"{BASE_URL}{endpoint}", timeout=15)
                logger.info(f"[streamyard] teams endpoint {endpoint} → {resp.status_code}")
                if resp.ok:
                    data = resp.json()
                    logger.info(f"[streamyard] teams data: {str(data)[:500]}")
                    teams = data.get("teams") or data.get("organizations") or (data if isinstance(data, list) else [])
                    if teams:
                        return teams[0].get("id")
            except Exception as e:
                logger.info(f"[streamyard] teams endpoint {endpoint} error: {e}")

        # Fall back to user info (print full response to find team field)
        for endpoint in ("/api/user", "/api/me"):
            try:
                resp = self.session.get(f"{BASE_URL}{endpoint}", timeout=15)
                if resp.ok:
                    data = resp.json()
                    logger.info(f"[streamyard] FULL user info: {data}")
                    self._workspace_id = data.get("primaryWorkspace")
                    team_id = (
                        data.get("primaryTeam")
                        or data.get("teamId")
                        or data.get("defaultTeamId")
                        or data.get("organizationId")
                        or (data.get("team") or {}).get("id")
                    )
                    if team_id:
                        return team_id
            except Exception:
                pass
        return None

    def is_authenticated(self) -> bool:
        """Check if we have a valid authenticated session."""
        if not self.csrf_token:
            return False
        try:
            for endpoint in ("/api/user", "/api/me", "/api/broadcasts?limit=1"):
                resp = self.session.get(f"{BASE_URL}{endpoint}", timeout=15)
                if resp.ok:
                    return True
            return False
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Broadcast listing
    # ------------------------------------------------------------------

    def list_broadcasts(self) -> list[dict]:
        """Return completed broadcasts, newest first."""
        self._get_team_id()  # populates self._workspace_id
        workspace_id = self._workspace_id
        if not workspace_id:
            raise RuntimeError("Could not determine StreamYard workspace ID")

        resp = self.session.get(
            f"{BASE_URL}/api/workspaces/{workspace_id}/broadcasts/list"
            "?limit=99&withEpisodeCount=true&withLastEpisodeId=true"
            "&wasCompleted=true&pinnedAsReusable=false",
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        broadcasts = data.get("broadcasts") or data.get("items") or (data if isinstance(data, list) else [])
        # Log first broadcast to help identify transcript fields
        if broadcasts:
            logger.info(f"[streamyard] sample broadcast keys: {list(broadcasts[0].keys())}")
        return broadcasts

    # ------------------------------------------------------------------
    # Video download
    # ------------------------------------------------------------------

    def _vod_url(self, broadcast_id: str, type_: str) -> str | None:
        """
        Shared helper: trigger VOD generation, poll until ready, return download URL.
        Tries workspace-based API first, then legacy API.
        Returns None if the endpoint is unavailable (404).
        """
        workspace_id = self._workspace_id
        bases = []
        if workspace_id:
            bases.append(f"{BASE_URL}/api/workspaces/{workspace_id}/broadcasts/{broadcast_id}")
        bases.append(f"{BASE_URL}/api/broadcasts/{broadcast_id}")

        headers = {"Referer": f"{BASE_URL}/broadcasts"}

        for base in bases:
            try:
                resp = self.session.post(
                    f"{base}/vod",
                    json={"csrfToken": self.csrf_token, "type": type_},
                    headers=headers,
                    timeout=20,
                )
                logger.info(f"[streamyard] POST {base}/vod type={type_} → {resp.status_code}")
                if not resp.ok:
                    continue

                # Poll until ready (up to 10 minutes)
                for _ in range(60):
                    time.sleep(10)
                    poll = self.session.get(f"{base}/vod", timeout=20)
                    if poll.ok and poll.json().get("status") != "creating":
                        break

                # Fetch download URLs
                urls_resp = self.session.get(
                    f"{base}/vod_download_urls?type={type_}",
                    timeout=20,
                )
                logger.info(f"[streamyard] GET vod_download_urls type={type_} → {urls_resp.status_code} {urls_resp.text[:200]}")
                if urls_resp.ok:
                    data = urls_resp.json()
                    return (data.get("videoUrl") or data.get("captionsUrl")
                            or data.get("transcriptUrl") or data.get("url"))
            except Exception as e:
                logger.info(f"[streamyard] _vod_url error for {base}: {e}")

        return None

    def get_transcript_url(self, broadcast_id: str) -> str | None:
        """Return a signed VTT URL from StreamYard, or None if unavailable."""
        for type_ in ("captions", "transcript"):
            url = self._vod_url(broadcast_id, type_)
            if url:
                return url
        return None

    def get_video_url(self, broadcast_id: str) -> str:
        """
        Triggers server-side generation of a download URL, polls until ready,
        then returns the direct video URL.
        """
        url = self._vod_url(broadcast_id, "video")
        if url:
            return url
        raise RuntimeError("Could not get video download URL from StreamYard")

    def download_video(
        self,
        broadcast_id: str,
        dest_path: Path,
        status_callback=None,
    ) -> Path:
        """
        Download a broadcast video to dest_path.
        Returns dest_path on success.
        """
        if dest_path.exists():
            if status_callback:
                status_callback(f"Video already downloaded: {dest_path.name}")
            return dest_path

        if status_callback:
            status_callback("Requesting download link from StreamYard...")

        video_url = self.get_video_url(broadcast_id)

        if status_callback:
            status_callback("Downloading video...")

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = dest_path.with_suffix(".tmp")

        with self.session.get(video_url, stream=True, timeout=None) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length", 0))
            downloaded = 0
            with open(tmp_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):  # 1 MB chunks
                    f.write(chunk)
                    downloaded += len(chunk)
                    if status_callback and total:
                        pct = int(downloaded * 100 / total)
                        mb = downloaded / 1024 / 1024
                        status_callback(f"Downloading... {pct}% ({mb:.0f} MB)")

        tmp_path.replace(dest_path)  # replace() overwrites if dest already exists

        if status_callback:
            mb = dest_path.stat().st_size / 1024 / 1024
            status_callback(f"Download complete ({mb:.0f} MB)")

        return dest_path
