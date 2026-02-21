from __future__ import annotations

import base64
import hashlib
import mimetypes
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote_to_bytes, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .models import AnswerOption, DownloadedAsset, QuestionContent, QuestionRecord


_STYLE_URL_PATTERN = re.compile(r"url\(\s*(['\"]?)(.+?)\1\s*\)", re.IGNORECASE)
_DATA_URL_PATTERN = re.compile(r"^data:(?P<mime>[^;,]+)?(?P<base64>;base64)?,(?P<data>.*)$", re.IGNORECASE | re.DOTALL)


class AssetDownloadError(RuntimeError):
    pass


class AssetDownloader:
    """Download and relink all media assets referenced from HTML fragments."""

    def __init__(
        self,
        session: requests.Session,
        *,
        site_base_url: str,
        timeout: float = 30.0,
        request_callable: Optional[Callable[..., requests.Response]] = None,
        anomaly_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self._session = session
        self._site_base_url = site_base_url
        self._timeout = timeout
        self._request_callable = request_callable
        self._anomaly_callback = anomaly_callback

    def rewrite_question_assets(
        self,
        record: QuestionRecord,
        question_dir: Path,
        *,
        question_key: Optional[str] = None,
    ) -> QuestionRecord:
        assets_dir = question_dir / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        question_id = record.metadata.question_id

        seen: Dict[str, DownloadedAsset] = {}
        assets: List[DownloadedAsset] = []

        prompt_html, prompt_assets = self._rewrite_html_fragment(
            record.content.prompt_html,
            assets_dir,
            seen,
            question_key=question_key,
            question_id=question_id,
        )
        stem_html, stem_assets = self._rewrite_html_fragment(
            record.content.stem_html,
            assets_dir,
            seen,
            question_key=question_key,
            question_id=question_id,
        )
        rationale_html, rationale_assets = self._rewrite_html_fragment(
            record.content.rationale_html,
            assets_dir,
            seen,
            question_key=question_key,
            question_id=question_id,
        )

        answer_options: List[AnswerOption] = []
        for option in record.content.answer_options:
            rewritten, option_assets = self._rewrite_html_fragment(
                option.content_html,
                assets_dir,
                seen,
                question_key=question_key,
                question_id=question_id,
            )
            answer_options.append(AnswerOption(letter=option.letter, content_html=rewritten))
            assets.extend(option_assets)

        assets.extend(prompt_assets)
        assets.extend(stem_assets)
        assets.extend(rationale_assets)

        rewritten_content = QuestionContent(
            prompt_html=prompt_html,
            stem_html=stem_html,
            answer_options=answer_options,
            rationale_html=rationale_html,
            correct_answers=list(record.content.correct_answers),
            question_type=record.content.question_type,
        )

        return QuestionRecord(
            metadata=record.metadata,
            source=record.source,
            content=rewritten_content,
            assets=assets,
            parse_warnings=list(record.parse_warnings),
            raw_table_row=dict(record.raw_table_row),
            raw_detail_payload=dict(record.raw_detail_payload),
            raw_payload=dict(record.raw_detail_payload),
            lifecycle=dict(record.lifecycle),
        )

    def _rewrite_html_fragment(
        self,
        html_fragment: str,
        assets_dir: Path,
        seen: Dict[str, DownloadedAsset],
        *,
        question_key: Optional[str],
        question_id: Optional[str],
    ) -> Tuple[str, List[DownloadedAsset]]:
        if not html_fragment:
            return "", []

        soup = BeautifulSoup(html_fragment, "html.parser")
        found_assets: List[DownloadedAsset] = []

        for tag in soup.find_all(True):
            for attr in ("src", "href", "data", "poster", "xlink:href"):
                if attr not in tag.attrs:
                    continue
                original = str(tag.attrs[attr]).strip()
                replacement, asset = self._resolve_and_store_asset(
                    original,
                    assets_dir,
                    seen,
                    question_key=question_key,
                    question_id=question_id,
                )
                if replacement:
                    tag.attrs[attr] = replacement
                if asset:
                    found_assets.append(asset)

            if "style" in tag.attrs:
                updated_style, style_assets = self._rewrite_style_urls(
                    str(tag.attrs["style"]),
                    assets_dir,
                    seen,
                    question_key=question_key,
                    question_id=question_id,
                )
                tag.attrs["style"] = updated_style
                found_assets.extend(style_assets)

            if "srcset" in tag.attrs:
                updated_srcset, srcset_assets = self._rewrite_srcset(
                    str(tag.attrs["srcset"]),
                    assets_dir,
                    seen,
                    question_key=question_key,
                    question_id=question_id,
                )
                tag.attrs["srcset"] = updated_srcset
                found_assets.extend(srcset_assets)

        return _serialize_fragment(soup), found_assets

    def _rewrite_style_urls(
        self,
        style: str,
        assets_dir: Path,
        seen: Dict[str, DownloadedAsset],
        *,
        question_key: Optional[str],
        question_id: Optional[str],
    ) -> Tuple[str, List[DownloadedAsset]]:
        assets: List[DownloadedAsset] = []

        def _replace(match: re.Match[str]) -> str:
            raw_url = match.group(2).strip()
            replacement, asset = self._resolve_and_store_asset(
                raw_url,
                assets_dir,
                seen,
                question_key=question_key,
                question_id=question_id,
            )
            if asset:
                assets.append(asset)
            if not replacement:
                return match.group(0)
            quote = match.group(1) or ""
            return f"url({quote}{replacement}{quote})"

        return _STYLE_URL_PATTERN.sub(_replace, style), assets

    def _rewrite_srcset(
        self,
        srcset: str,
        assets_dir: Path,
        seen: Dict[str, DownloadedAsset],
        *,
        question_key: Optional[str],
        question_id: Optional[str],
    ) -> Tuple[str, List[DownloadedAsset]]:
        assets: List[DownloadedAsset] = []
        parts = [part.strip() for part in srcset.split(",") if part.strip()]
        rewritten_parts: List[str] = []

        for part in parts:
            tokens = part.split()
            if not tokens:
                continue
            replacement, asset = self._resolve_and_store_asset(
                tokens[0],
                assets_dir,
                seen,
                question_key=question_key,
                question_id=question_id,
            )
            if asset:
                assets.append(asset)
            tokens[0] = replacement or tokens[0]
            rewritten_parts.append(" ".join(tokens))

        return ", ".join(rewritten_parts), assets

    def _resolve_and_store_asset(
        self,
        url: str,
        assets_dir: Path,
        seen: Dict[str, DownloadedAsset],
        *,
        question_key: Optional[str],
        question_id: Optional[str],
    ) -> Tuple[Optional[str], Optional[DownloadedAsset]]:
        if not url:
            return None, None

        if url.startswith(("#", "javascript:", "mailto:", "tel:")):
            return None, None

        if url in seen:
            return seen[url].local_path, None

        try:
            if url.startswith("data:"):
                asset = self._store_data_uri(url, assets_dir)
                seen[url] = asset
                return asset.local_path, asset

            absolute_url = self._normalize_url(url)
            if not absolute_url:
                self._report_anomaly(
                    {
                        "category": "unsupported_asset_url",
                        "summary": "Unsupported or non-normalizable asset URL",
                        "severity": "warning",
                        "details": {"url": url},
                        "question_key": question_key,
                        "question_id": question_id,
                        "source": "asset",
                        "recommended_action": "Add URL normalization support for this asset reference pattern.",
                    }
                )
                return None, None

            asset = self._download_remote_asset(absolute_url, assets_dir)
            if not asset.mime_type or asset.local_path.endswith(".bin"):
                self._report_anomaly(
                    {
                        "category": "unknown_asset_type",
                        "summary": "Asset downloaded with unknown MIME type/extension",
                        "severity": "info",
                        "details": {
                            "url": absolute_url,
                            "mime_type": asset.mime_type,
                            "local_path": asset.local_path,
                        },
                        "question_key": question_key,
                        "question_id": question_id,
                        "source": "asset",
                        "recommended_action": "Review file signature and extend MIME/extension mapping rules.",
                    }
                )
            seen[url] = asset
            return asset.local_path, asset
        except (AssetDownloadError, OSError, RuntimeError, ValueError, requests.RequestException) as exc:
            self._report_anomaly(
                {
                    "category": "asset_download_error",
                    "summary": "Asset download failed",
                    "severity": "error",
                    "details": {"url": url, "error": str(exc), "error_type": exc.__class__.__name__},
                    "question_key": question_key,
                    "question_id": question_id,
                    "source": "asset",
                    "recommended_action": "Inspect asset URL/referer behavior and add fallback download logic.",
                }
            )
            return None, None

    def _normalize_url(self, url: str) -> Optional[str]:
        if url.startswith("//"):
            return f"https:{url}"

        parsed = urlparse(url)
        if parsed.scheme in {"http", "https"}:
            return url

        if not parsed.scheme:
            return urljoin(self._site_base_url, url)

        return None

    def _download_remote_asset(self, url: str, assets_dir: Path) -> DownloadedAsset:
        if self._request_callable:
            response = self._request_callable(method="GET", url=url, timeout=self._timeout)
        else:
            response = self._session.get(url, timeout=self._timeout)
            response.raise_for_status()

        content = response.content
        digest = hashlib.sha256(content).hexdigest()
        content_type = _clean_content_type(response.headers.get("Content-Type"))
        suffix = _guess_suffix(url, content_type)
        filename = f"{digest}{suffix}"

        destination = assets_dir / filename
        if not destination.exists():
            destination.write_bytes(content)

        return DownloadedAsset(
            original_url=url,
            local_path=f"assets/{filename}",
            source_type="remote",
            mime_type=content_type,
            size_bytes=len(content),
            sha256=digest,
        )

    def _store_data_uri(self, data_uri: str, assets_dir: Path) -> DownloadedAsset:
        match = _DATA_URL_PATTERN.match(data_uri)
        if not match:
            raise AssetDownloadError("Invalid data URI encountered in question content")

        mime_type = match.group("mime") or "application/octet-stream"
        encoded_data = match.group("data")
        is_base64 = bool(match.group("base64"))

        if is_base64:
            payload = base64.b64decode(encoded_data, validate=False)
        else:
            payload = unquote_to_bytes(encoded_data)

        digest = hashlib.sha256(payload).hexdigest()
        suffix = _guess_suffix("", mime_type)
        filename = f"{digest}{suffix}"
        destination = assets_dir / filename

        if not destination.exists():
            destination.write_bytes(payload)

        return DownloadedAsset(
            original_url=data_uri,
            local_path=f"assets/{filename}",
            source_type="data_uri",
            mime_type=mime_type,
            size_bytes=len(payload),
            sha256=digest,
        )

    def _report_anomaly(self, payload: Dict[str, Any]) -> None:
        if self._anomaly_callback:
            self._anomaly_callback(payload)


def _clean_content_type(content_type: Optional[str]) -> Optional[str]:
    if not content_type:
        return None
    return content_type.split(";", 1)[0].strip().lower()


def _guess_suffix(url: str, mime_type: Optional[str]) -> str:
    if mime_type:
        guessed = mimetypes.guess_extension(mime_type)
        if guessed == ".jpe":
            return ".jpg"
        if guessed:
            return guessed

    parsed = urlparse(url)
    path = parsed.path
    suffix = Path(path).suffix
    if suffix:
        return suffix

    return ".bin"


def _serialize_fragment(soup: BeautifulSoup) -> str:
    if soup.body:
        return "".join(str(child) for child in soup.body.contents)
    return "".join(str(child) for child in soup.contents)
