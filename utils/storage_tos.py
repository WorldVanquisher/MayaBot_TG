# utils/storage_tos.py
# -*- coding: utf-8 -*-
import os
import uuid

# 先尝试从 .env 加载环境变量
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import tos


class TosStorage:
    """
    一个超薄封装：bytes -> TOS 对象 -> 公开 URL
    依赖以下环境变量：
        TOS_ACCESS_KEY
        TOS_SECRET_KEY
        TOS_ENDPOINT   (例如 https://tos-cn-beijing.volces.com)
        TOS_REGION     (例如 cn-beijing)
        TOS_BUCKET     (例如 fortune-bot-media)
    """

    def __init__(
        self,
        ak: str | None = None,
        sk: str | None = None,
        endpoint: str | None = None,
        region: str | None = None,
        bucket: str | None = None,
        prefix: str = "bot",
    ):
        self.ak = ak or os.getenv("TOS_ACCESS_KEY")
        self.sk = sk or os.getenv("TOS_SECRET_KEY")
        self.endpoint = endpoint or os.getenv("TOS_ENDPOINT")
        self.region = region or os.getenv("TOS_REGION")
        self.bucket = bucket or os.getenv("TOS_BUCKET")
        self.prefix = prefix.strip("/")

        if not all([self.ak, self.sk, self.endpoint, self.region, self.bucket]):
            raise RuntimeError(
                "TOS 配置不完整: 请设置 "
                "TOS_ACCESS_KEY / TOS_SECRET_KEY / TOS_ENDPOINT / TOS_REGION / TOS_BUCKET"
            )

        self.client = tos.TosClientV2(self.ak, self.sk, self.endpoint, self.region)

        domain = self.endpoint.replace("https://", "").replace("http://", "")
        self.base_url = f"https://{self.bucket}.{domain}"

    def upload_bytes(self, data: bytes, suffix: str = ".jpg") -> str:
        """上传一段 bytes 到 TOS，返回 https URL"""
        if not suffix.startswith("."):
            suffix = "." + suffix
        key = f"{self.prefix}/{uuid.uuid4().hex}{suffix}"

        resp = self.client.put_object(self.bucket, key, content=data)
        if resp.status_code != 200:
            raise RuntimeError(f"TOS put_object failed: {resp.status_code}")

        return f"{self.base_url}/{key}"
