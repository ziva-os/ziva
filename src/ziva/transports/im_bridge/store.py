"""IM bridge config + route-table persistence (``~/.ziva/config.yaml``).

The IM bridge section is stored under the ``im_bridge`` key of the global Ziva
config file. It is **global** (not per-workspace): IM messages arrive from a
phone and have no natural tie to whichever workspace happens to be active. The
``routes`` map ``(channel, account_id, chat_id) → sid`` so one IM conversation
keeps its ziva session across messages.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict

import yaml

from ziva.storage.file_storage import get_base_dir

from ziva.transports.im_bridge.models import ChannelConfig


@dataclass
class IMConfig:
    default_workspace: str | None = None
    allowed_senders: list[str] = field(default_factory=list)
    channels: Dict[str, ChannelConfig] = field(default_factory=lambda: {
        "feishu": ChannelConfig(),
        "telegram": ChannelConfig(),
    })
    # route_key (channel:account_id:chat_id) → ziva session id
    routes: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def _config_path(cls) -> Path:
        return get_base_dir() / "config.yaml"

    @classmethod
    def _load_yaml(cls) -> Dict[str, Any]:
        path = cls._config_path()
        if not path.exists():
            return {}
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    @classmethod
    def load(cls) -> "IMConfig":
        data = cls._load_yaml()
        im_data = data.get("im_bridge")
        if not isinstance(im_data, dict):
            im_data = {}

        channels_raw = im_data.get("channels") or {}
        channels: Dict[str, ChannelConfig] = {
            "feishu": ChannelConfig(),
            "telegram": ChannelConfig(),
        }
        for name, cfg in channels_raw.items():
            if name not in channels or not isinstance(cfg, dict):
                continue
            channels[name] = ChannelConfig(
                enabled=bool(cfg.get("enabled", False)),
                app_id=cfg.get("app_id"),
                app_secret=cfg.get("app_secret"),
                account_id=cfg.get("account_id"),
                gateway_url=cfg.get("gateway_url"),
                bot_token=cfg.get("bot_token"),
                proxy_url=cfg.get("proxy_url"),
            )
        return cls(
            default_workspace=im_data.get("default_workspace"),
            allowed_senders=list(im_data.get("allowed_senders") or []),
            channels=channels,
            routes=dict(im_data.get("routes") or {}),
        )

    def save(self) -> None:
        data = {
            "default_workspace": self.default_workspace,
            "allowed_senders": list(self.allowed_senders),
            "channels": {k: asdict(v) for k, v in self.channels.items()},
            "routes": dict(self.routes),
        }
        self._save_to_yaml(data)

    @classmethod
    def _save_to_yaml(cls, im_data: Dict[str, Any]) -> None:
        """Merge the IM bridge section into the global config.yaml atomically."""
        config_path = cls._config_path()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        full_config = cls._load_yaml()
        full_config["im_bridge"] = im_data

        # Atomic write: tmp file + rename.
        tmp = config_path.with_suffix(f".yaml.{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(
            yaml.dump(
                full_config,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        tmp.replace(config_path)

    # -- route table ---------------------------------------------------------

    def route_for(self, route_key: str) -> str | None:
        return self.routes.get(route_key)

    def set_route(self, route_key: str, sid: str) -> None:
        self.routes[route_key] = sid
        self.save()

    # -- redaction for the API surface --------------------------------------

    def to_public_dict(self) -> Dict[str, Any]:
        """Config with secrets redacted, safe to return to the frontend."""
        chans: Dict[str, Any] = {}
        for name, cfg in self.channels.items():
            d = asdict(cfg)
            for secret in cfg.secret_fields():
                if d.get(secret):
                    d[secret] = "••••••"  # present but hidden
            chans[name] = d
        return {
            "default_workspace": self.default_workspace,
            "allowed_senders": list(self.allowed_senders),
            "channels": chans,
        }
