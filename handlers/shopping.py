"""Shopping list API handlers.

Voice-driven shared shopping list. Items are added by voice command
or web form, viewed and checked off at the store via web page.

Storage: JSON file at ``shopping.json`` next to ``server.json``.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import json
import logging
import os
import threading
import time
from typing import Any
from datetime import datetime

logger: logging.Logger = logging.getLogger("glowup.shopping")

# ---------------------------------------------------------------------------
# Shopping list store — thread-safe JSON file
# ---------------------------------------------------------------------------

# Item schema:
#   {
#       "id": int,          — auto-increment
#       "text": str,        — item description
#       "added": float,     — timestamp
#       "checked": bool,    — crossed off
#       "checked_at": float — when checked (for auto-clear)
#   }

# Checked items are auto-cleared after this many seconds.
_CHECKED_TTL_S: float = 86400.0  # 24 hours.

# Next ID counter key in the JSON root.
_NEXT_ID_KEY: str = "_next_id"


class ShoppingStore:
    """Thread-safe shopping list backed by a JSON file.

    Args:
        path: Path to the JSON file.
    """

    def __init__(self, path: str) -> None:
        """Initialize the shopping store."""
        self._path: str = path
        self._lock: threading.Lock = threading.Lock()
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        """Load the store from disk."""
        if os.path.exists(self._path):
            try:
                with open(self._path, "r") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as exc:
                logger.error("Failed to load shopping list: %s", exc)
        return {_NEXT_ID_KEY: 1, "items": []}

    def _save(self) -> None:
        """Write the store to disk.  Caller must hold _lock."""
        try:
            with open(self._path, "w") as f:
                json.dump(self._data, f, indent=2)
        except IOError as exc:
            logger.error("Failed to save shopping list: %s", exc)

    def _prune_checked(self) -> None:
        """Remove items checked more than _CHECKED_TTL_S ago.

        Caller must hold _lock.
        """
        now: float = time.time()
        self._data["items"] = [
            item for item in self._data["items"]
            if not item.get("checked")
            or (now - item.get("checked_at", 0)) < _CHECKED_TTL_S
        ]

    def get_items(self) -> list[dict[str, Any]]:
        """Return all items, pruning stale checked items.

        Returns:
            List of item dicts.
        """
        with self._lock:
            self._prune_checked()
            self._save()
            return list(self._data["items"])

    def add_item(self, text: str) -> dict[str, Any]:
        """Add an item to the list.

        Args:
            text: Item description.

        Returns:
            The created item dict.
        """
        with self._lock:
            item_id: int = self._data.get(_NEXT_ID_KEY, 1)
            item: dict[str, Any] = {
                "id": item_id,
                "text": text.strip(),
                "added": time.time(),
                "checked": False,
                "checked_at": None,
            }
            self._data["items"].append(item)
            self._data[_NEXT_ID_KEY] = item_id + 1
            self._save()
            return item

    def check_item(self, item_id: int, checked: bool = True) -> bool:
        """Check or uncheck an item.

        Args:
            item_id: Item ID.
            checked: True to check, False to uncheck.

        Returns:
            True if item was found and updated.
        """
        with self._lock:
            for item in self._data["items"]:
                if item["id"] == item_id:
                    item["checked"] = checked
                    item["checked_at"] = time.time() if checked else None
                    self._save()
                    return True
        return False

    def remove_item(self, item_id: int) -> bool:
        """Remove an item by ID.

        Args:
            item_id: Item ID.

        Returns:
            True if item was found and removed.
        """
        with self._lock:
            before: int = len(self._data["items"])
            self._data["items"] = [
                i for i in self._data["items"] if i["id"] != item_id
            ]
            if len(self._data["items"]) < before:
                self._save()
                return True
        return False

    def remove_by_text(self, text: str) -> bool:
        """Remove the first unchecked item matching text (case-insensitive).

        Args:
            text: Item text to match.

        Returns:
            True if an item was found and removed.
        """
        target: str = text.strip().lower()
        with self._lock:
            for i, item in enumerate(self._data["items"]):
                if (item["text"].lower() == target
                        and not item.get("checked")):
                    self._data["items"].pop(i)
                    self._save()
                    return True
        return False

    def has_item(self, text: str) -> bool:
        """Check if an unchecked item matching text exists.

        Args:
            text: Item text to search for (case-insensitive).

        Returns:
            True if found.
        """
        target: str = text.strip().lower()
        return any(
            item["text"].lower() == target
            and not item.get("checked")
            for item in self._data["items"]
        )

    def clear_checked(self) -> int:
        """Remove all checked items.

        Returns:
            Number of items removed.
        """
        with self._lock:
            before: int = len(self._data["items"])
            self._data["items"] = [
                i for i in self._data["items"]
                if not i.get("checked")
            ]
            removed: int = before - len(self._data["items"])
            if removed:
                self._save()
            return removed

    def clear_all(self) -> int:
        """Remove all items.

        Returns:
            Number of items removed.
        """
        with self._lock:
            count: int = len(self._data["items"])
            self._data["items"] = []
            self._save()
            return count

    def unchecked_count(self) -> int:
        """Return count of unchecked items."""
        return sum(
            1 for i in self._data["items"]
            if not i.get("checked")
        )


# ---------------------------------------------------------------------------
# Handler mixin
# ---------------------------------------------------------------------------

class ShoppingHandlerMixin:
    """Shopping list API endpoints for GlowUpRequestHandler."""

    def _handle_get_shopping_page(self) -> None:
        """GET /shopping — serve the shopping list web page."""
        page_path: str = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "static", "shopping.html",
        )
        try:
            with open(page_path, "r") as f:
                html: str = f.read()
            body: bytes = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self._send_json(404, {"error": "Shopping page not found"})

    def _handle_get_shopping(self) -> None:
        """GET /api/shopping — return all shopping list items."""
        store: ShoppingStore = self.server._shopping_store
        items: list[dict[str, Any]] = store.get_items()
        self._send_json(200, {"items": items})

    def _handle_post_shopping(self) -> None:
        """POST /api/shopping — add an item.

        Body: ``{"text": "milk"}``
        """
        store: ShoppingStore = self.server._shopping_store
        body: dict[str, Any] = self._read_json_body()
        text: str = body.get("text", "").strip()
        if not text:
            self._send_json(400, {"error": "Missing 'text' field"})
            return
        item: dict[str, Any] = store.add_item(text)
        logger.info("Shopping: added '%s'", text)
        self._send_json(201, {"item": item})

    def _handle_post_shopping_check(self, item_id_str: str) -> None:
        """POST /api/shopping/{id}/check — check or uncheck an item.

        Body: ``{"checked": true}``
        """
        store: ShoppingStore = self.server._shopping_store
        try:
            item_id: int = int(item_id_str)
        except ValueError:
            self._send_json(400, {"error": "Invalid item ID"})
            return
        body: dict[str, Any] = self._read_json_body()
        checked: bool = body.get("checked", True)
        if store.check_item(item_id, checked):
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"error": "Item not found"})

    def _handle_delete_shopping_item(self, item_id_str: str) -> None:
        """DELETE /api/shopping/{id} — remove an item."""
        store: ShoppingStore = self.server._shopping_store
        try:
            item_id: int = int(item_id_str)
        except ValueError:
            self._send_json(400, {"error": "Invalid item ID"})
            return
        if store.remove_item(item_id):
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"error": "Item not found"})

    def _handle_delete_shopping_checked(self) -> None:
        """DELETE /api/shopping/checked — remove all checked items."""
        store: ShoppingStore = self.server._shopping_store
        removed: int = store.clear_checked()
        self._send_json(200, {"status": "ok", "removed": removed})
