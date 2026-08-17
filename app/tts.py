from __future__ import annotations

import base64
import binascii
from pathlib import Path
from typing import Any

import httpx


TTS_PROVIDERS = {
    "moneyprinter": "MoneyPrinterTurbo 内置 TTS",
    "openai": "OpenAI 兼容 TTS API",
    "gpt_sovits": "GPT-SoVITS",
}


def _audio_bytes(response: httpx.Response) -> bytes:
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").lower()
    if "json" in content_type:
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("TTS 服务返回了无法解析的 JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("TTS 服务没有返回音频")
        encoded = payload.get("audio") or payload.get("audio_base64") or payload.get("data")
        if not isinstance(encoded, str):
            raise RuntimeError(str(payload.get("error") or payload.get("message") or "TTS 服务没有返回音频"))
        try:
            return base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise RuntimeError("TTS 服务返回的音频编码无效") from exc
    return response.content


def _write_audio(response: httpx.Response, output_path: Path) -> Path:
    audio = _audio_bytes(response)
    if len(audio) < 128:
        raise RuntimeError("TTS 服务返回的音频内容为空或不完整")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(audio)
    return output_path


def synthesize(
    provider: str,
    text: str,
    output_path: Path,
    params: dict[str, Any],
    settings: dict[str, str],
) -> Path:
    if provider == "openai":
        url = str(settings.get("tts_api_url") or "").strip().rstrip("/")
        if not url:
            raise RuntimeError("未配置 OpenAI 兼容 TTS API 地址")
        if not url.endswith("/audio/speech"):
            url = f"{url}/audio/speech"
        headers = {"Accept": "audio/mpeg"}
        api_key = str(settings.get("tts_api_key") or "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {
            "model": str(settings.get("tts_model") or "gpt-4o-mini-tts"),
            "input": text,
            "voice": str(params.get("tts_voice") or settings.get("tts_voice") or "alloy"),
            "response_format": "mp3",
            "speed": float(params.get("voice_rate") or 1.0),
        }
        with httpx.Client(timeout=180) as client:
            response = client.post(url, headers=headers, json=payload)
        return _write_audio(response, output_path)

    if provider == "gpt_sovits":
        url = str(settings.get("gpt_sovits_url") or "").strip()
        ref_audio = str(settings.get("gpt_sovits_ref_audio") or "").strip()
        if not url:
            raise RuntimeError("未配置 GPT-SoVITS API 地址")
        if not ref_audio:
            raise RuntimeError("未配置 GPT-SoVITS 参考音频路径")
        payload = {
            "text": text,
            "text_lang": str(settings.get("gpt_sovits_text_lang") or "zh"),
            "ref_audio_path": ref_audio,
            "prompt_lang": str(settings.get("gpt_sovits_prompt_lang") or "zh"),
            "prompt_text": str(settings.get("gpt_sovits_prompt_text") or ""),
            "speed_factor": float(params.get("voice_rate") or 1.0),
        }
        with httpx.Client(timeout=300) as client:
            response = client.post(url, json=payload)
        return _write_audio(response, output_path)

    raise RuntimeError(f"不支持的 TTS 提供商: {provider}")


def tts_configuration_error(provider: str, settings: dict[str, str]) -> str:
    if provider not in TTS_PROVIDERS:
        return "不支持的配音引擎"
    if provider == "openai" and not str(settings.get("tts_api_url") or "").strip():
        return "请先在分析设置中填写 OpenAI 兼容 TTS API 地址"
    if provider == "gpt_sovits":
        if not str(settings.get("gpt_sovits_url") or "").strip():
            return "请先在分析设置中填写 GPT-SoVITS API 地址"
        if not str(settings.get("gpt_sovits_ref_audio") or "").strip():
            return "请先在分析设置中填写 GPT-SoVITS 参考音频路径"
    return ""
