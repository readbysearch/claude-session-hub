"""
HTTP uploader for the Claude Session Hub daemon.
Sends batches of JSONL lines to the server.
"""
import logging

import requests

logger = logging.getLogger("csh-daemon")


class Uploader:
    def __init__(self, server_url: str, api_key: str, batch_size: int = 200):
        self.server_url = server_url.rstrip("/")
        self.api_key = api_key
        self.batch_size = batch_size
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {api_key}"
        self.session.headers["Content-Type"] = "application/json"

    def upload(self, project_path: str, session_uuid: str, lines: list[dict]) -> bool:
        """
        Upload lines in batches. Returns True if all batches succeeded.
        Each line is {"line_number": int, "raw_json": dict}.
        """
        url = f"{self.server_url}/api/upload"
        success = True

        for i in range(0, len(lines), self.batch_size):
            batch = lines[i : i + self.batch_size]
            payload = {
                "project_path": project_path,
                "session_uuid": session_uuid,
                "lines": batch,
            }
            try:
                resp = self.session.post(url, json=payload, timeout=30)
                if resp.status_code == 200:
                    data = resp.json()
                    logger.debug(
                        f"Uploaded batch: {data.get('inserted', 0)} inserted "
                        f"/ {data.get('total_lines', 0)} total"
                    )
                else:
                    logger.error(f"Upload failed ({resp.status_code}): {resp.text}")
                    success = False
            except requests.RequestException as e:
                logger.error(f"Upload error: {e}")
                success = False

        return success
