import html
import json
import asyncio
import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Callable, Coroutine, Any, NamedTuple, Union
from functools import partial

from anki.notes import Note, NoteId
from anki.collection import Collection, OpChanges
from anki.decks import DeckId
from aqt import mw
from aqt.browser import Browser
from aqt.operations import CollectionOp
from aqt.utils import showWarning, tooltip
from collections.abc import Container, Iterable, Sequence

from .api_client import (
    ANTHROPIC,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_RETRY_WAIT_SECONDS,
    GEMINI,
    OPENAI,
    TOGETHER,
    PauseState,
    begin_run,
    cancel_run,
    close_all_sessions,
    end_run,
    is_cancelled,
    join_run,
    pause_state,
    post_with_retry,
    rate_limit_tracker,
    run_cancelled,
    run_is_cancelled,
    run_paused,
    set_connection_pool_size,
    take_stop_reason,
    wait_while_paused,
)
from .terminal_client import get_response_from_terminal, is_terminal_model
from .chain_types import (
    STEP_CANCELLED,
    STEP_COMPLETED,
    STEP_STOPPED,
    ChainStep,
    StepOutcome,
)
from .collection_access import RunCancelled, begin_cleanup_phase, end_cleanup_phase
from .concurrency import TASK_QUEUE_DEPTH, ConcurrencyGate, executor_size
from .diagnostics import (
    clear_cancel_time,
    diagnostic_level,
    dump_thread_stacks,
    note_cancel_time,
    seconds_since_cancel,
    start_cancel_watchdog,
)
from .progress_controls import (
    disable_run_controls,
    rearm_cleanup_cancel,
    refresh_run_controls,
    start_run_controls,
)
from .step_failure import failed_step_outcome

from ..call_logging import bulk_op_logging, phase_log
from ..utils import get_field_config, print_error_traceback


logger = logging.getLogger(__name__)

MAX_TOKENS_VALUE = 8000
# Shortest gap between progress dialog redraws. Redraws run on Anki's main thread, so this is
# what keeps a burst of finishing tasks from starving the UI.
PROGRESS_UPDATE_INTERVAL = 0.15
# How long the cleanup waits for the main thread to grey the run controls, and to re-arm the
# dialog's cancel. The main thread is idle during a run, so this is only reached if it is stuck
# on something else.
CLEANUP_REARM_TIMEOUT = 2.0
DEFAULT_SYSTEM_INSTRUCTION = (
    "You are a helpful assistant for processing Japanese text. You are a"
    " superlative expert in the Japanese language and its writing system."
    " You are designed to output JSON."
)

OPENAI_FIXED_TEMPERATURE_MODEL_PREFIXES = (
    "gpt-5",
    "o1",
    "o3",
    "o4",
)

ANTHROPIC_FIXED_TEMPERATURE_MODEL_PREFIXES = (
    "claude-fable-5",
    "claude-mythos-5",
    "claude-mythos-preview",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-sonnet-5",
)


# The running operation's shared thread pool, so the gate can size it once it knows what
# memory allows. One bulk op runs at a time - Anki's progress dialog owns the UI for the length
# of one - so a module-level reference is the same lifetime as the run itself, and is cleared
# in run_bulk_op's finally either way.
_run_executor: Optional[ThreadPoolExecutor] = None


def set_run_executor(executor: Optional[ThreadPoolExecutor]) -> None:
    """Register (or forget) the pool `resize_run_executor` may grow."""
    global _run_executor
    _run_executor = executor


def resize_run_executor(ceiling: int) -> None:
    """Let the run's pool spawn as many workers as the gate's ceiling now warrants.

    Upward only, and for the same reason the connection pool is only told about rises: threads
    already spawned cannot be taken back, and a pool wider than the ceiling costs nothing once
    the burst that widened it has passed. What it must not do is stay at the backstop - see
    `executor_size` for the 516-thread pool that produced.

    `_max_workers` is private, but it is what `ThreadPoolExecutor.submit` consults on every
    call to decide whether to start another worker, so raising it is exactly the supported
    behaviour with no supported spelling. Guarded, because a pool that will not be resized is
    a pool that spawns as many threads as it did before rather than a broken run.
    """
    executor = _run_executor
    if executor is None:
        return
    wanted = executor_size(ceiling)
    try:
        if wanted > executor._max_workers:
            logger.debug(
                "Growing the run's thread pool %d -> %d workers", executor._max_workers, wanted
            )
            executor._max_workers = wanted
    except Exception as e:
        logger.debug("Could not resize the run's thread pool: %s", e)


def size_pools_to_ceiling(ceiling: int) -> None:
    """Everything sized off the gate's ceiling, moved together when the ceiling moves.

    The connection pool and the thread pool are both created before the gate has read the
    machine, so both start at a guess and both have to be told. Passed to the gate as its
    `on_ceiling_changed`, and called once by hand as soon as the gate exists, because the gate
    only reports changes to a ceiling it has already computed.
    """
    set_connection_pool_size(ceiling)
    resize_run_executor(ceiling)


def log_phase(label: str, started: float, **extra) -> float:
    """Log how long a shutdown or cleanup step took, and return a fresh start marker.

    Cancellation problems show up as one of these steps blocking on something, and which step
    it is narrows the cause down immediately. Debug logging shows them for every run; once a
    run has been cancelled they are logged at info level too, since that is the case where
    knowing which step is slow actually matters and a cancelled run only emits a handful.
    """
    now = time.monotonic()
    level = diagnostic_level()
    if logger.isEnabledFor(level):
        details = "".join(f" {k}={v}" for k, v in extra.items())
        since_cancel = seconds_since_cancel()
        if since_cancel is not None:
            details += f" since_cancel={since_cancel:.1f}s"
        logger.log(level, "[phase] %s took %.3fs%s", label, now - started, details)
    return now


class CancelState:
    """Shared state for cancellation that can be accessed across threads."""

    def __init__(self):
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def is_cancelled(self):
        return self._cancelled


class DialogCancelState:
    """The progress dialog's cancel, as a cancel state for wait_while_paused.

    Escape or the dialog's close box only sets the dialog's flag; nothing turns it into a
    cancel of the run while no async phase is running (CancelManager does that, and only
    during run_plans_rolling). A pause wait on the op thread that watched the run alone would
    hold a sync op, or the gap between two phases, until someone resumed it.
    """

    def is_cancelled(self) -> bool:
        return bool(mw.progress.want_cancel())


def get_response(
    model: str,
    prompt: str,
    cancel_state: Optional[CancelState] = None,
    instructions: Optional[str] = None,
    response_schema: Optional[dict] = None,
    max_output_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    json_result_corrector: Optional[Callable[[str], str]] = None,
    effort: Optional[str] = None,
) -> Union[dict, None]:
    """Get a response from the appropriate model based on the configuration.

    Args:
        model: The model to use for the request.

    Returns:
        A dict containing the parsed JSON response, or None if there was an error.
    """
    if is_terminal_model(model):
        config = mw.addonManager.getConfig(__name__)
        if config is None:
            logger.error("No configuration found for the addon.")
            return None
        return get_response_from_terminal(
            model,
            prompt,
            config,
            cancel_state=cancel_state,
            instructions=instructions or DEFAULT_SYSTEM_INSTRUCTION,
            response_schema=response_schema,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            json_result_corrector=json_result_corrector,
        )
    elif model.startswith("gemini"):
        return get_response_from_gemini(
            model,
            prompt,
            cancel_state=cancel_state,
            instructions=instructions,
            response_schema=response_schema,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            json_result_corrector=json_result_corrector,
        )
    elif model.startswith("gpt") or model.startswith("o3") or model.startswith("o1"):
        return get_response_from_openai(
            model,
            prompt,
            cancel_state=cancel_state,
            instructions=instructions,
            response_schema=response_schema,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            json_result_corrector=json_result_corrector,
        )
    elif model.startswith("claude") or model.startswith("anthropic"):
        return get_response_from_anthropic(
            model,
            prompt,
            cancel_state=cancel_state,
            instructions=instructions,
            response_schema=response_schema,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            json_result_corrector=json_result_corrector,
            effort=effort,
        )
    elif "/" in model:
        return get_response_from_together(
            model,
            prompt,
            cancel_state=cancel_state,
            instructions=instructions,
            response_schema=response_schema,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            json_result_corrector=json_result_corrector,
        )
    else:
        logger.error(f"Unsupported model: {model}")
        return None


def post_to_api(
    provider: str,
    model: str,
    url: str,
    headers: dict,
    data: dict,
    config: dict,
    cancel_state: Optional[CancelState] = None,
):
    """Send a request to a provider, waiting out any rate limits it reports.

    Blocking, and always called from a worker thread. Returns the final response, or None if
    the request was cancelled or never got a response.
    """
    return post_with_retry(
        provider=provider,
        model=model,
        url=url,
        headers=headers,
        json_body=data,
        timeout=config.get("request_timeout", 300),
        cancel_state=cancel_state,
        max_retries=int(config.get("max_request_retries", DEFAULT_MAX_RETRIES)),
        max_retry_wait=float(config.get("max_retry_wait_seconds", DEFAULT_MAX_RETRY_WAIT_SECONDS)),
    )


def decode_json_result(json_str: str):
    logging.debug("json_result", json_str)
    try:
        result = json.loads(json_str)
        logger.debug(
            "Parsed result from json: %s", json.dumps(result, ensure_ascii=False, indent=2)
        )
        return result
    except json.JSONDecodeError:
        logger.error(f"Failed to parse JSON response, json_result: {json_str}")
        return None
    except ValueError as ve:
        logger.error(f"Failed to parse JSON response - ValueError: {ve}")
        if "integer string conversion" in str(ve):
            logger.error("Large integer detected in JSON response")
        logger.error(f"json_result: {json_str}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error parsing JSON: {e}")
        logger.error(f"json_result: {json_str}")
        return None


def clean_response_schema_for_gemini(schema: dict) -> dict:
    """Cleans the response schema to be compatible with Gemini's expected format.

    Args:
        response_schema: The original response schema.

    Returns:
        A cleaned response schema compatible with Gemini.
    """
    # Clean "additionalProperties" from array items and objects to avoid Gemini rejecting the schema
    if isinstance(schema, dict):
        if schema.get("type") == "array" and "items" in schema:
            items = schema["items"]
            if isinstance(items, dict):
                if "additionalProperties" in items:
                    del items["additionalProperties"]
                # Recursively clean items
                schema["items"] = clean_response_schema_for_gemini(items)
        elif schema.get("type") == "object" and "properties" in schema:
            if "additionalProperties" in schema:
                del schema["additionalProperties"]
            for key, value in schema["properties"].items():
                schema["properties"][key] = clean_response_schema_for_gemini(value)
    return schema


def openai_supports_custom_temperature(model: str) -> bool:
    return not model.startswith(OPENAI_FIXED_TEMPERATURE_MODEL_PREFIXES)


def anthropic_supports_custom_temperature(model: str) -> bool:
    normalized_model = model
    if model.startswith("anthropic/"):
        normalized_model = model.split("/", 1)[1]
    return not normalized_model.startswith(ANTHROPIC_FIXED_TEMPERATURE_MODEL_PREFIXES)


def anthropic_response_indicates_unsupported_temperature(response_text: str) -> bool:
    text = response_text.lower()
    return "temperature" in text and (
        "deprecated for this model" in text or "not supported for this model" in text
    )


def get_response_from_gemini(
    model: str,
    prompt: str,
    cancel_state: Optional[CancelState] = None,
    instructions: Optional[str] = None,
    response_schema: Optional[dict] = None,
    max_output_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    json_result_corrector: Optional[Callable[[str], str]] = None,
) -> Union[dict, None]:
    """Get a response from Google's Gemini API.

    Args:
        prompt: The prompt to send to the API.

    Returns:
        A dict containing the parsed JSON response, or None if there was an error.
    """
    logger.debug(f"Gemini call, model: {model}")

    if is_cancelled(cancel_state):
        return None

    # Create the request body
    data: dict[str, Any] = {
        "contents": [
            {
                # "role": "user",
                "parts": [
                    {"text": prompt},
                ],
            },
        ],
        "system_instruction": {
            "parts": [
                {
                    "text": (
                        instructions
                        if instructions
                        else (
                            "You are a helpful assistant for processing Japanese text. You are a"
                            " superlative expert in the Japanese language and its writing system."
                            " You are designed to output JSON."
                        )
                    )
                },
            ]
        },
        "generationConfig": {
            "responseMimeType": "application/json",
            # maxOutputTokens includes both thinking and output so it needs to be large enough
            "maxOutputTokens": max_output_tokens or MAX_TOKENS_VALUE,
            "thinkingConfig": {"thinkingBudget": 6000},
        },
    }
    if max_output_tokens is not None:
        logger.debug("Using max_output_tokens %d", max_output_tokens)
    if temperature is not None:
        data["generationConfig"]["temperature"] = temperature
        logger.debug("Using temperature %s", temperature)
    if response_schema:
        # On a copy: the caller's schema is shared with requests to other providers, which
        # need the additionalProperties this removes
        response_schema = clean_response_schema_for_gemini(json.loads(json.dumps(response_schema)))
        data["generationConfig"]["responseSchema"] = response_schema
        logger.debug(
            "Using response schema %s", json.dumps(response_schema, ensure_ascii=False, indent=2)
        )

    headers = {
        "Content-Type": "application/json",
        # "x-goog-api-key": google_api_key,
    }

    config = mw.addonManager.getConfig(__name__)
    if config is None:
        print("No configuration found for the addon.")
        return None
    google_api_key = config.get("google_api_key", "")

    # Make the API call
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={google_api_key}"
    )
    response = post_to_api(
        provider=GEMINI,
        model=model,
        url=url,
        headers=headers,
        data=data,
        config=config,
        cancel_state=cancel_state,
    )
    if response is None:
        return None

    if response.status_code != 200:
        logger.error(f"Error: {response.status_code}, {response.text}")
        return None

    try:
        decoded_json = json.loads(response.text)
        # Extract content from Gemini response structure
        content_text = decoded_json["candidates"][0]["content"]["parts"][0]["text"]
    except json.JSONDecodeError as je:
        logger.error(f"Error decoding JSON: {je}")
        logger.error("response %s", response.text)
        return None
    except KeyError as ke:
        logger.error(f"Error extracting content: {ke}")
        logger.error("response %s", response.text)
        return None

    # Extract the JSON from the response
    json_result = extract_json_string(content_text)

    result = decode_json_result(json_result)
    if not result and json_result_corrector:
        json_result = json_result_corrector(json_result)
        result = decode_json_result(json_result)
    return result


def get_response_from_openai(
    model: str,
    prompt: str,
    cancel_state: Optional[CancelState] = None,
    instructions: Optional[str] = None,
    response_schema: Optional[dict] = None,
    max_output_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    json_result_corrector: Optional[Callable[[str], str]] = None,
) -> Union[dict, None]:
    logger.debug("OpenAI call, model %s", model)

    if is_cancelled(cancel_state):
        return None

    # Use max_completion_tokens instead of max_tokens for o3

    messages = [
        {
            "role": "system",
            "content": (
                instructions
                if instructions
                else (
                    "You are a helpful assistant for processing Japanese text. You are a"
                    " superlative expert in the Japanese language and its writing system. You are"
                    " designed to output JSON."
                )
            ),
        },
        {"role": "user", "content": prompt},
    ]
    config = mw.addonManager.getConfig(__name__)
    if config is None:
        logger.error("No configuration found for the addon.")
        return None
    openai_api_key = config.get("openai_api_key", "")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {openai_api_key}",
    }

    data: dict[str, Any] = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": messages,
    }
    if any(model.startswith(m) for m in ["o3", "o1", "gpt-5"]):
        # This is total completion tokens, including both reasoning and output
        data["max_completion_tokens"] = max_output_tokens or MAX_TOKENS_VALUE
    else:
        # This is for GPT models and only limits output, not reasoning
        data["max_tokens"] = max_output_tokens or MAX_TOKENS_VALUE
    if temperature is not None:
        if openai_supports_custom_temperature(model):
            data["temperature"] = temperature
            logger.debug("Using temperature %s", temperature)
        else:
            logger.debug(
                "Skipping custom temperature %s for model %s because this OpenAI chat"
                " model family only accepts the default temperature.",
                temperature,
                model,
            )
    if response_schema:
        data["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "schema_name",
                "schema": response_schema,
                "strict": True,
            },
        }
        logger.debug(
            "Using response schema %s", json.dumps(response_schema, ensure_ascii=False, indent=2)
        )

    # Make the API call
    response = post_to_api(
        provider=OPENAI,
        model=model,
        url="https://api.openai.com/v1/chat/completions",
        headers=headers,
        data=data,
        config=config,
        cancel_state=cancel_state,
    )
    if response is None:
        return None

    if response.status_code != 200:
        logger.error(f"Error: {response.status_code}, {response.text}")
        return None

    try:
        decoded_json = json.loads(response.text)
        content_text = decoded_json["choices"][0]["message"]["content"]
    except json.JSONDecodeError as je:
        logger.error(f"Error decoding JSON: {je}")
        logger.error("response %s", response.text)
        return None
    except KeyError as ke:
        logger.error(f"Error extracting content: {ke}")
        logger.error("response %s", response.text)
        return None

    # Extract the cleaned meaning from the response
    json_result = extract_json_string(content_text)

    result = decode_json_result(json_result)
    if not result and json_result_corrector:
        json_result = json_result_corrector(json_result)
        result = decode_json_result(json_result)
    return result


def get_response_from_together(
    model: str,
    prompt: str,
    cancel_state: Optional[CancelState] = None,
    instructions: Optional[str] = None,
    response_schema: Optional[dict] = None,
    max_output_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    json_result_corrector: Optional[Callable[[str], str]] = None,
) -> Union[dict, None]:
    logger.debug("Together AI call, model %s", model)

    if is_cancelled(cancel_state):
        return None

    messages = [
        {
            "role": "system",
            "content": (
                instructions
                if instructions
                else (
                    "You are a helpful assistant for processing Japanese text. You are a"
                    " superlative expert in the Japanese language and its writing system. You are"
                    " designed to output JSON."
                )
            ),
        },
        {"role": "user", "content": prompt},
    ]
    config = mw.addonManager.getConfig(__name__)
    if config is None:
        logger.error("No configuration found for the addon.")
        return None
    together_api_key = config.get("together_api_key", "")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {together_api_key}",
    }

    data: dict[str, Any] = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": messages,
        "max_tokens": max_output_tokens or MAX_TOKENS_VALUE,
    }
    if temperature is not None:
        data["temperature"] = temperature
        logger.debug("Using temperature %s", temperature)

    # Make the API call
    response = post_to_api(
        provider=TOGETHER,
        model=model,
        url="https://api.together.xyz/v1/chat/completions",
        headers=headers,
        data=data,
        config=config,
        cancel_state=cancel_state,
    )
    if response is None:
        return None

    if response.status_code != 200:
        logger.error(f"Error: {response.status_code}, {response.text}")
        return None

    try:
        decoded_json = json.loads(response.text)
        content_text = decoded_json["choices"][0]["message"]["content"]
    except json.JSONDecodeError as je:
        logger.error(f"Error decoding JSON: {je}")
        logger.error("response %s", response.text)
        return None
    except KeyError as ke:
        logger.error(f"Error extracting content: {ke}")
        logger.error("response %s", response.text)
        return None

    json_result = extract_json_string(content_text)

    result = decode_json_result(json_result)
    if not result and json_result_corrector:
        json_result = json_result_corrector(json_result)
        result = decode_json_result(json_result)
    return result


def get_response_from_anthropic(
    model: str,
    prompt: str,
    cancel_state: Optional[CancelState] = None,
    instructions: Optional[str] = None,
    response_schema: Optional[dict] = None,
    max_output_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    json_result_corrector: Optional[Callable[[str], str]] = None,
    effort: Optional[str] = None,
) -> Union[dict, None]:
    """Get a response from Anthropic's Claude API.

    Args:
        prompt: The prompt to send to the API.

    Returns:
        A dict containing the parsed JSON response, or None if there was an error.
    """
    logger.debug("Anthropic call, model %s", model)

    if is_cancelled(cancel_state):
        return None

    messages = [
        {"role": "user", "content": prompt},
    ]
    # Create the request body
    data: dict[str, Any] = {
        "model": model,
        "system": (
            instructions
            if instructions
            else (
                "You are a helpful assistant for processing Japanese text. You are a"
                " superlative expert in the Japanese language and its writing system. You are"
                " designed to output JSON."
            )
        ),
        "max_tokens": max_output_tokens or MAX_TOKENS_VALUE,
        "messages": messages,
    }
    use_temperature = temperature is not None and anthropic_supports_custom_temperature(model)
    if use_temperature:
        data["temperature"] = temperature
        logger.debug("Using temperature %s", temperature)
    elif temperature is not None:
        logger.debug(
            "Skipping custom temperature %s for model %s because this Anthropic model family"
            " only accepts the default temperature.",
            temperature,
            model,
        )

    if response_schema:
        data["output_config"] = {
            "format": {
                "type": "json_schema",
                "schema": response_schema,
            }
        }
        logger.debug(
            "Using response schema %s", json.dumps(response_schema, ensure_ascii=False, indent=2)
        )

    if effort:
        # Extended thinking, as the Gemini call already asks for, but these models size the
        # budget themselves and take an effort level instead of a token count. It shares
        # output_config with the schema, so this runs after that is built rather than before.
        # A custom temperature is not accepted alongside thinking, so it is removed.
        data["thinking"] = {"type": "adaptive"}
        data.setdefault("output_config", {})["effort"] = effort
        data.pop("temperature", None)
        logger.debug("Using adaptive thinking at %s effort", effort)

    config = mw.addonManager.getConfig(__name__)
    if config is None:
        logger.error("No configuration found for the addon.")
        return None
    anthropic_api_key = config.get("anthropic_api_key", "")

    headers = {
        "x-api-key": anthropic_api_key,
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }

    # Make the API call
    url = "https://api.anthropic.com/v1/messages"
    response = post_to_api(
        provider=ANTHROPIC,
        model=model,
        url=url,
        headers=headers,
        data=data,
        config=config,
        cancel_state=cancel_state,
    )
    if response is None:
        return None

    if (
        response.status_code == 400
        and "temperature" in data
        and anthropic_response_indicates_unsupported_temperature(response.text)
    ):
        logger.warning(
            "Anthropic model %s rejected custom temperature; retrying with default temperature.",
            model,
        )
        retry_data = dict(data)
        retry_data.pop("temperature", None)

        response = post_to_api(
            provider=ANTHROPIC,
            model=model,
            url=url,
            headers=headers,
            data=retry_data,
            config=config,
            cancel_state=cancel_state,
        )
        if response is None:
            return None

    if response.status_code != 200:
        logger.error(f"Error: {response.status_code}, {response.text}")
        return None

    try:
        decoded_json = json.loads(response.text)
        content_blocks = decoded_json.get("content", [])
        text_blocks = [
            block.get("text", "")
            for block in content_blocks
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        content_text = "\n".join([text for text in text_blocks if text]).strip()
        if not content_text:
            raise KeyError("content text blocks")
    except json.JSONDecodeError as je:
        logger.error(f"Error decoding JSON: {je}")
        logger.error("response %s", response.text)
        return None
    except KeyError as ke:
        logger.error(f"Error extracting content: {ke}")
        logger.error("response %s", response.text)
        return None

    # Extract the cleaned meaning from the response
    json_result = extract_json_string(content_text)

    result = decode_json_result(json_result)
    if not result and json_result_corrector:
        json_result = json_result_corrector(json_result)
        result = decode_json_result(json_result)
    return result


def extract_json_string(content_text):
    # Add logic to extract the cleaned meaning from the GPT response
    # You may need to parse the JSON or use other string manipulation techniques
    # based on the structure of the response.

    # For simplicity, let's assume that the stuff asked for is surrounded by curly braces in the
    # response.
    # Find the first occurrence of "{" and the last occurrence of "}" in the response.
    start_index = content_text.find("{")
    end_index = content_text.rfind("}")

    if start_index != -1 and end_index != -1:
        return content_text[start_index : end_index + 1]
    else:
        print("Did not return JSON parseable result")
        return content_text


class CancelManager:
    """
    A class to manage cancellation of asynchronous operations.
    It provides a way to set and check cancellation requests.
    """

    def __init__(
        self,
        tasks,
        cancel_state: Optional[CancelState] = None,
        progress_updater: Optional["AsyncTaskProgressUpdater"] = None,
    ):
        self.cancel_requested = False
        self.tasks = tasks  # Live tasks to cancel if needed; the rolling driver mutates it
        # Set once no more tasks will be started. The rolling driver refills its live set as
        # tasks finish, so an all-done set is normally the moment just before the next ones
        # start rather than the end of the run - without this the monitor would exit on the
        # first such gap and stop polling for cancellation for the rest of the run.
        self.work_complete = False
        self.cancel_state = cancel_state or CancelState()
        self.progress_updater = progress_updater
        # Lets whoever is waiting on the tasks stop waiting the moment cancel is requested,
        # rather than having to see every task through to the end
        self.cancelled_event = asyncio.Event()
        self.monitor_task = asyncio.create_task(self.monitor_for_cancellation())

    async def wait_cancelled(self) -> None:
        await self.cancelled_event.wait()

    def request_cancel(self):
        """Request cancellation of all tasks managed by this instance."""
        started = time.monotonic()
        pending = sum(1 for t in self.tasks if not t.done())
        logger.info(
            "Cancellation requested: %d tasks (%d still running), %d threads alive",
            len(self.tasks),
            pending,
            threading.active_count(),
        )
        self.cancel_requested = True
        self.cancelled_event.set()
        note_cancel_time()
        # What the worker threads are doing at the moment of the cancel, and then repeatedly
        # until they are gone. Everything that has made cancelling slow so far has been work
        # continuing in threads nothing was waiting for, and this is what shows it.
        dump_thread_stacks("at cancel")
        start_cancel_watchdog()

        # Set the cancelled state first: this is what actually stops the work. Requests that
        # have not been sent yet are skipped, cooldown waits wake up, requests already in
        # flight have their sockets shut down so the threads waiting on them return at once,
        # and any response that still arrives afterwards is discarded instead of being acted
        # on. cancel_run() is the one that matters - the ops overwhelmingly do not pass their
        # cancel_state down, so without it their abandoned threads keep calling APIs and
        # querying the collection.
        cancel_run()
        self.cancel_state.cancel()

        # Drops the pooled connections so nothing new reuses them. The in-flight ones were
        # already aborted by cancel_run() above.
        close_all_sessions()

        # Show the cancelling message and stop every other progress update from here on. A
        # cancelled run unwinds hundreds of tasks at once, and each one asking the main thread
        # to redraw the dialog is what made the window lock up instead of closing.
        if self.progress_updater is not None:
            self.progress_updater.show_cancelling()
        else:
            mw.taskman.run_on_main(
                lambda: mw.progress.update(
                    label="<b>Cancelling operations...</b><br>Finishing up.",
                    value=0,
                    max=0,  # Indeterminate progress
                )
            )

        started = log_phase("cancel: show_cancelling", started)

        # Cancel all tasks without waiting
        for task in self.tasks:
            if not task.done():
                task.cancel()
        self.monitor_task.cancel()
        log_phase("cancel: cancel all tasks", started)

    def is_cancel_requested(self) -> bool:
        """Check if cancellation has been requested."""
        return self.cancel_requested

    def mark_work_complete(self) -> None:
        """Say that no more tasks will be started, so the monitor may stop once they drain."""
        self.work_complete = True

    async def monitor_for_cancellation(self):
        """Monitor for cancellation requests and cancel all tasks if requested."""
        try:
            while not self.cancel_requested:
                # Check for cancellation request from Anki, or from the run's own work (the
                # claude CLI stops the run when the subscription's usage limit is hit)
                if mw.progress.want_cancel() or run_cancelled():
                    logger.debug("Cancellation requested, setting cancel_requested to True")
                    self.request_cancel()
                    break

                # Check if all tasks are completed naturally
                if self.work_complete and all(task.done() for task in self.tasks):
                    logger.debug("All tasks completed naturally, exiting monitor")
                    break

                # Check frequently but don't hog the CPU
                await asyncio.sleep(0.1)

        except asyncio.CancelledError:
            logger.debug("Cancellation monitor task cancelled")
            # Just exit the task when cancelled


def drain_task_errors(tasks: "Sequence[asyncio.Task]") -> None:
    """Read the results of finished tasks so nothing they raised goes unreported.

    Neither asyncio.wait nor a done callback reads its tasks' results, so anything a finished
    task raised is still sitting in it unretrieved. process_op handles its own errors, so
    reaching here with one means something got past it and is worth seeing rather than being
    reported much later against a closed loop.
    """
    for task in tasks:
        if task.done() and not task.cancelled():
            error = task.exception()
            if error is not None:
                logger.error("Task failed: %s", error)
                print_error_traceback(error, logger)


async def wait_for_completions(
    done_queue: "asyncio.Queue[asyncio.Task]",
    cancel_manager: CancelManager,
) -> "list[asyncio.Task]":
    """Wait until at least one task has finished, and return every task that has.

    Completions arrive on a queue fed by each task's done callback rather than by handing the
    whole live set to asyncio.wait: waking on every single completion is the whole point of
    the rolling driver, and asyncio.wait adds and then removes a callback on every task it is
    given, so doing it once per completion would cost a pass over the live set thousands of
    times a run.

    Returns an empty list if cancellation was requested first. Waiting for the tasks to unwind
    is not safe to rely on: a task blocked in a worker thread detaches when cancelled, but
    nothing can interrupt the blocking HTTP request itself, and any task that swallows the
    cancellation or is stuck on something uninterruptible would hold the whole run open -
    which is what made cancelling hang for minutes on one straggler. So on cancellation we
    simply stop waiting. The tasks are cancelled and abandoned; their requests finish in their
    own threads and the results are discarded (post_with_retry throws away anything that
    arrives after a cancel), and the run moves on to saving what it already has.
    """
    finished: "list[asyncio.Task]" = []
    if done_queue.empty():
        # A future rather than awaiting the queue directly: this has to be able to lose the
        # race to cancellation without swallowing a completion, and a cancelled Queue.get
        # takes nothing off the queue.
        next_done: "asyncio.Future[Any]" = asyncio.ensure_future(done_queue.get())
        cancel_waiter: "asyncio.Future[Any]" = asyncio.ensure_future(
            cancel_manager.wait_cancelled()
        )
        try:
            await asyncio.wait({next_done, cancel_waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            cancel_waiter.cancel()
            if next_done.done() and not next_done.cancelled():
                finished.append(next_done.result())
            else:
                next_done.cancel()
    # Whatever else piled up while we were away, so a batch of completions costs one wake-up
    while not done_queue.empty():
        finished.append(done_queue.get_nowait())
    return finished


def format_pause_line(
    pause: PauseState, tasks_in_progress: int = 0, now: Optional[float] = None
) -> str:
    """The dialog's line saying the run is paused, why, and until when.

    The tasks counted are the ones holding a gate slot. During a manual pause they are
    finishing their requests; during a usage-limit pause most of them are waiting to retry the
    request that hit the limit, so the wording has to be true of both.
    """
    line = f"<b>Paused</b>: {html.escape(pause.reason)}"
    if pause.automatic and pause.resume_at is not None:
        if now is None:
            now = time.time()
        minutes = max(0, math.ceil((pause.resume_at - now) / 60))
        resume_clock = time.strftime("%H:%M", time.localtime(pause.resume_at))
        line += f", resuming at {resume_clock} (in {minutes} min)"
    if tasks_in_progress > 0:
        tasks = "1 task" if tasks_in_progress == 1 else f"{tasks_in_progress} tasks"
        line += f", {tasks} finishing or waiting to retry"
    return line


def show_dialog_label(label: str, value: int, maximum: int) -> None:
    """Redraw the progress dialog and its run controls. Main thread only."""
    mw.progress.update(label=label, value=value, max=maximum)
    refresh_run_controls()


class AsyncTaskProgressUpdater:
    """A class to update the progress dialog in async ops."""

    def __init__(
        self, total_notes: Optional[int] = None, total_tasks: int = 0, title: Optional[str] = None
    ):
        self.total_tasks = total_tasks
        self.tasks_done = 0
        self.tasks_in_progress = 0
        self.notes_done = 0
        self.total_notes = total_notes
        # Sum of each task's execution time, for estimating average time per task
        self.cumulative_task_time = 0.0
        self.max_task_time = 0.0
        self.start_time = time.time()
        # How much of the time since start_time the run spent paused, left out of the ETA: the
        # tasks still to do will not spend it again. Counted by the periodic tick, so it is
        # accurate to about a second at each end of a pause.
        self.paused_seconds = 0.0
        # When the periodic tick last ran (time.monotonic()); None until its first tick
        self._last_tick: Optional[float] = None
        # Counters are incremented from both the event loop and executor threads
        self._counts_lock = threading.Lock()
        # Set by the bulk ops so the dialog can show what the concurrency gate is doing
        self.gate: Optional[ConcurrencyGate] = None
        # Every finishing task asks for a redraw, so bursts have to be coalesced - see _push
        self._ui_lock = threading.Lock()
        self._update_pending = False
        self._last_update_at = 0.0
        # Set by begin_cleanup_stage: the next label is the stage's first (see _push)
        self._stage_starting = False
        # The adding's last counts, for end_cleanup_cancel to redraw without the Cancel hint
        self._adding_counts: Optional[tuple[int, int, int]] = None
        self._suppressed = False
        # Set once the main thread has reset the dialog's flag for the cleanup's note adding,
        # cleared once nothing is left for that cancel to stop
        self._cleanup_cancel_armed = threading.Event()
        # Which re-arm the op thread still wants; bumped when it stops waiting for one or ends
        # the adding's cancel, so a re-arm landing after that does nothing. Under _arm_lock,
        # which the main thread's check-and-reset holds as well.
        self._arm_lock = threading.Lock()
        self._arm_generation = 0
        # Put before every title this updater shows, phase titles included; see set_title_prefix
        self.title_prefix = ""
        # What the dialog was last told to show, prefix included, for show_title
        self._shown_title = ""
        if title is None:
            title = "Processing asynchronous tasks..."
        self.set_title(title)

        # Periodic updater (started when an event loop is running)
        self.autoupdate_task: Optional[asyncio.Task] = None
        self._autoupdate_started = False
        self._autoupdate_deferred = False
        self.start_autoupdate()

    def start_autoupdate(self):
        """Start periodic progress updates if an event loop is running."""
        if self.autoupdate_task and not self.autoupdate_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop yet; allow caller to retry later from async context
            self._autoupdate_deferred = True
            return
        self.autoupdate_task = loop.create_task(self._periodic_update_progress())
        self._autoupdate_started = True
        self._autoupdate_deferred = False

    async def _periodic_update_progress(self):
        # Each bulk op starts and stops its own ticker; the gap between two of them (the next
        # phase's planning, or a pause between phases) is not this ticker's to count
        self._last_tick = None
        while True:
            self._tick(time.monotonic())
            await asyncio.sleep(1)

    def _tick(self, now: float) -> None:
        """One beat of the periodic redraw, at `now` (time.monotonic()).

        The time since the last beat counts as paused if the run is paused at this one. The
        ticker runs on the op thread's event loop, which is enrolled in the run, so run_paused
        answers for it - and is also the poll that ends an automatic pause whose time has come.
        """
        last, self._last_tick = self._last_tick, now
        if last is not None and run_paused():
            with self._counts_lock:
                self.paused_seconds += now - last
        self.update_progress()

    def stop_autoupdate(self):
        """Stop the periodic progress update task."""
        if self.autoupdate_task:
            self.autoupdate_task.cancel()
            self.autoupdate_task = None

    def set_total_notes(self, total_notes: int):
        self.total_notes = total_notes

    def set_total_tasks(self, total_tasks: int):
        self.total_tasks = total_tasks

    def set_title(self, title: str):
        """Set the dialog's title, and keep it as the base a phase title is built on."""
        self.title = title
        self._show_title(title)

    def set_title_prefix(self, prefix: str) -> None:
        """Start every title shown from now on with `prefix` (a chain's "Step 2/5: ").

        Kept apart from the title so that it outlives whatever titles the dialog later: a
        multi-phase op's `begin_phase` titles each phase from `title`, and so would anything
        that calls `set_title` during the run.
        """
        self.title_prefix = prefix
        self._show_title(self.title)

    def show_title(self) -> None:
        """Draw the last title again. Main thread, once the progress dialog exists.

        The op modules build their updater before `selected_notes_op` starts the progress
        dialog, and aqt's `set_title` does nothing while there is no dialog, so the title
        given to the constructor is never seen unless it is drawn again after the start.
        """
        title = self._shown_title
        mw.taskman.run_on_main(lambda: mw.progress.set_title(title))

    def _show_title(self, title: str) -> None:
        shown = self.title_prefix + title
        self._shown_title = shown
        mw.taskman.run_on_main(lambda: mw.progress.set_title(shown))

    def begin_phase(self, index: int, total: int, name: str = "") -> None:
        """Start one phase of a multi-phase op: title it, and reset the per-run counters.

        Each phase is a whole bulk op of its own but they all share one updater, so without
        this a later phase would start with the earlier ones' figures. Both halves of the
        estimate are ratios over the whole run - the ETA is elapsed time over tasks done, the
        average is cumulative task time over the same - so a phase of quick local work after
        one of slow API calls would show an ETA of hours for a run of seconds, and the task
        bar would start part-filled with work that is already over.
        """
        with self._counts_lock:
            self.total_tasks = 0
            self.tasks_done = 0
            self.tasks_in_progress = 0
            self.notes_done = 0
            self.cumulative_task_time = 0.0
            self.max_task_time = 0.0
            self.start_time = time.time()
            # Paused time is time since start_time, so it starts over with it
            self.paused_seconds = 0.0
            self._last_tick = None
        phase_title = f"{self.title} (Phase {index}/{total}"
        phase_title += f": {name})" if name else ")"
        self._show_title(phase_title)

    def _push(self, label: str, value: int, maximum: int, force: bool = False) -> None:
        """Queue a dialog redraw, collapsing bursts into a single update.

        Progress is refreshed from every task as it finishes, so a run with a high
        concurrency limit produces hundreds of these at once - and a cancelled run produces
        them all in the same instant, as every task unwinds together. Each one has to be run
        on the main thread, so letting them all through buries Anki's event loop: the window
        stops repainting and the click on Cancel isn't even seen for a long time.

        At most one redraw is ever queued, and only one every PROGRESS_UPDATE_INTERVAL, so a
        burst of any size costs the main thread exactly one update.
        """
        with self._ui_lock:
            if self._suppressed and not force:
                return
            if self._stage_starting:
                # A cleanup stage's first label, drawn even right after the stage before's
                # last and over a redraw of it still queued, which then draws first. Dropped,
                # the dialog showed the stage before through the whole of a slow first step:
                # the first add_note's hooks under "Processing new notes".
                self._stage_starting = False
                self._update_pending = False
                force = True
            if self._update_pending:
                return
            now = time.time()
            if not force and now - self._last_update_at < PROGRESS_UPDATE_INTERVAL:
                return
            self._update_pending = True
            self._last_update_at = now

        def run_update():
            try:
                # The buttons are refreshed with the label, so they follow a pause or resume
                # made anywhere - an automatic one included - within one redraw
                show_dialog_label(label, value, maximum)
            finally:
                with self._ui_lock:
                    self._update_pending = False

        mw.taskman.run_on_main(run_update)

    def show_cancelling(self) -> None:
        """Switch the dialog to the cancelling message and stop all further updates."""
        with self._ui_lock:
            self._suppressed = False
            self._update_pending = False
        self._push(
            "<b>Cancelling operations...</b><br>Finishing up.",
            value=0,
            maximum=0,  # Indeterminate progress
            force=True,
        )
        with self._ui_lock:
            self._suppressed = True
        # Pause or Resume on a run being cancelled would do nothing, and a Resume would look
        # like it undid the cancel. Also covers a cancel the run's own work made, which does
        # not set the dialog's flag the buttons otherwise watch.
        mw.taskman.run_on_main(disable_run_controls)

    def begin_cleanup(self) -> None:
        """Let the cleanup draw its progress again, and grey the buttons while it writes.

        The suppression `show_cancelling` sets is for the burst of redraws while every task
        unwinds at once; by cleanup the drivers have returned and nothing bursts. Left on, a
        cancelled run's dialog would say "Finishing up" through the whole of note adding and
        new-note processing, which a cancel of the API work does not skip.

        Nothing a cancel could stop runs until the note adding, which arms a cancel of its own
        (`arm_cleanup_cancel`), and only when there are notes to add: the edited notes' write
        before it can be long (a translate or kanjify run over thousands of notes), and a live
        Cancel through it would do nothing.

        Returns once the grey has landed on the main thread, so that the caller's read of the
        dialog's flag hears every press made while Cancel still said it cancels the run, and
        none after (a greyed Cancel takes no clicks, and `swallows_cancel` drops Escape). Read
        before the grey, a press in between was counted by a run with nothing to add and lost
        by one whose adding reset the flag. The wait gives up after CLEANUP_REARM_TIMEOUT.
        """
        with self._ui_lock:
            self._suppressed = False
        greyed = threading.Event()

        def grey() -> None:
            try:
                disable_run_controls()
            finally:
                greyed.set()

        mw.taskman.run_on_main(grey)
        if not greyed.wait(CLEANUP_REARM_TIMEOUT):
            logger.warning(
                "The main thread did not grey the run controls within %.0f s; a cancel pressed"
                " before it does may be lost",
                CLEANUP_REARM_TIMEOUT,
            )

    def arm_cleanup_cancel(self, total_notes: int) -> None:
        """Give the note adding a cancel of its own, and say so in the dialog as it goes live.

        Adding can be slow (every `add_note` runs the note_will_be_added hooks), so a second
        cancel stops it. The dialog's flag still holds the first, so it is reset on the main
        thread (`rearm_cleanup_cancel`), and this thread waits for that: reading the flag
        before the reset lands would take the first cancel for a second one and add nothing.
        Done for every run with notes to add, cancelled or not, so a finished run's adding can
        be stopped too.

        `cleanup_cancel_requested` hears the flag only once the main thread has really reset
        it: without a dialog, or if the reset failed, the flag may still hold the first cancel,
        and trusting it once dropped every prepared note. The adding just cannot be cancelled
        then. If the main thread gets to the reset only after this thread stopped waiting, or
        after the adding ended, it does nothing: resetting the flag then could erase a cancel
        pressed in between, and the adding would be told of a cancel after it was over.

        The adding's label is drawn in the same main-thread step, so the dialog stops saying
        "Cancelling operations" as Cancel comes back, and says Cancel stops the adding only if
        it does.

        The flag is reset in a run that was not cancelled too. Set then, it holds a press made
        after the API work, while the results were flushed or the edited notes written, with
        Cancel still live for part of it and saying it cancels the run. Kept, it stopped the
        adding and dropped every note the run had paid for, where the same press a moment
        earlier cancels the run and adds them all: a press is the run's cancel until Cancel
        says it stops the adding.
        """
        # The label rearm draws, for end_cleanup_cancel to take the hint off: a cancel before
        # the first note is added leaves it the adding's last
        self._adding_counts = (0, total_notes, 0)
        landed = threading.Event()
        with self._arm_lock:
            self._arm_generation += 1
            generation = self._arm_generation

        def rearm():
            with self._arm_lock:
                if self._arm_generation != generation:
                    return
                if rearm_cleanup_cancel():
                    self._cleanup_cancel_armed.set()
                # Under the lock, so this thread's give-up below sees the check-and-reset
                # either done or not begun
                landed.set()
            try:
                show_dialog_label(
                    self._note_adding_label(0, total_notes, 0), value=0, maximum=total_notes
                )
            except Exception as e:
                logger.error("Error drawing the note adding progress: %s", e)

        mw.taskman.run_on_main(rearm)
        if landed.wait(CLEANUP_REARM_TIMEOUT):
            return
        with self._arm_lock:
            if landed.is_set():
                return
            self._arm_generation += 1
        logger.warning(
            "The main thread did not re-arm the cancel within %.0f s; the adding cannot be"
            " cancelled",
            CLEANUP_REARM_TIMEOUT,
        )

    def cleanup_cancel_requested(self) -> bool:
        """Whether the user cancelled the cleanup's note adding, since `arm_cleanup_cancel`.

        The dialog's flag only, not `run_cancelled()`, which holds the cancel of the API work
        and stays set for the rest of the run. Only while the cancel is armed: until the main
        thread has reset it, the flag may still hold that first cancel.
        """
        return self._cleanup_cancel_armed.is_set() and bool(mw.progress.want_cancel())

    def end_cleanup_cancel(self) -> None:
        """Grey the buttons again, once nothing is left in the cleanup that a cancel stops.

        Resolving the added notes' ids and unlinking the ones not added always run to the
        end, as they are what leaves the word arrays consistent; a live Cancel during them
        would look like it could stop them.
        """
        with self._arm_lock:
            # A re-arm still queued on the main thread must not arm the cancel again
            self._arm_generation += 1
            self._cleanup_cancel_armed.clear()
        counts, self._adding_counts = self._adding_counts, None
        if counts is not None:
            # Its last label said Cancel stops the adding, and stays up until a later stage
            # draws, which a run with nothing to resolve or unlink may not have
            with self._ui_lock:
                self._update_pending = False
            self._push(self._note_adding_label(*counts), counts[0], counts[1], force=True)
        mw.taskman.run_on_main(disable_run_controls)

    def begin_cleanup_stage(self) -> None:
        """Restart the clock for one stage of the cleanup (deduping, adding, resolving ids).

        The cleanup's labels measure their time and ETA from `start_time`, which until this
        was last set when the API phase began: after an hour of requests, adding ten notes
        showed an hour gone and an average of minutes per note. Called by the cleanup before
        each stage, not by the stage's own progress calls, which repeat. The stage's first label
        is drawn whatever the redraw throttle says (`_push`).

        The API phase's task and note counters stay as they are: no cleanup label reads them.
        The paused time is only reset, not tracked: the cleanup cannot be paused.
        """
        with self._counts_lock:
            self.start_time = time.time()
            self.paused_seconds = 0.0
        with self._ui_lock:
            self._stage_starting = True

    def show_paused(self, detail: str = "") -> None:
        """Draw the pause into the dialog now, for a wait that nothing else redraws.

        Between two phases the phase before has stopped its ticker, so without this the
        dialog would keep that phase's last figures and the buttons would never change. Draws
        nothing when the run is not paused or is being cancelled.
        """
        # Ends an automatic pause that is already over, rather than showing it
        run_paused()
        pause = pause_state()
        if pause is None:
            return
        label = format_pause_line(pause)
        if detail:
            label += f"<br>{html.escape(detail)}"
        with self._ui_lock:
            if self._suppressed:
                return
            # A redraw still queued from the phase before would otherwise swallow this one,
            # and nothing else draws until the resume. Both run, in order.
            self._update_pending = False
        # A full bar rather than a busy one: the phase before is done and nothing is running
        self._push(label, value=1, maximum=1, force=True)

    def increment_counts(
        self,
        total_tasks=0,
        tasks_done: int = 0,
        tasks_in_progress: int = 0,
        notes_done: int = 0,
        cumulative_task_time: float = 0.0,
    ):
        """Increment the counts of tasks and notes."""
        with self._counts_lock:
            self.total_tasks += total_tasks
            self.tasks_done += tasks_done
            self.tasks_in_progress += tasks_in_progress
            self.notes_done += notes_done
            self.cumulative_task_time += cumulative_task_time
            if cumulative_task_time > self.max_task_time:
                self.max_task_time = cumulative_task_time

    def update_progress(self):
        """Update the Step 1 progress dialog with the current task and note counts."""
        task_progress_msg = f"""<strong>Processing:</strong>
            <br><strong><code>{self.tasks_done}/{self.total_tasks}</code></strong>
            tasks <small style="opacity: 0.85"> | Waiting response: {self.tasks_in_progress}</small>
            """
        if self.gate is not None:
            task_progress_msg += (
                f'<br><small style="opacity: 0.85">Running: {self.gate.status_text()}</small>'
            )
        if self.total_notes is not None:
            tasks_per_note = (
                round(self.tasks_done / self.notes_done, 1) if self.notes_done > 0 else 0
            )
            task_progress_msg += (
                f"<br><strong><code>{self.notes_done}/{self.total_notes}</code></strong> notes"
                f' <small style="opacity: 0.85"> | Avg tasks per note: {tasks_per_note}</small>'
            )

        # Ends an automatic pause whose time has come before showing it, as a waiter would
        run_paused()
        pause = pause_state()
        if pause is not None:
            pause_msg = format_pause_line(pause, self.tasks_in_progress)
            task_progress_msg = f"{pause_msg}<br>{task_progress_msg}"

        # "Time" is the wall clock since the phase began, pauses included, so it matches what
        # the user has been waiting; the estimate works from the time spent working
        elapsed_s = time.time() - self.start_time
        working_s = max(0.0, elapsed_s - self.paused_seconds)
        elapsed_time = time.strftime("%H:%M:%S", time.gmtime(elapsed_s))
        time_msg = f"<br><code>Time: {elapsed_time}</code>"
        if self.paused_seconds >= 1:
            paused_time = time.strftime("%H:%M:%S", time.gmtime(self.paused_seconds))
            time_msg += f" <small>(paused {paused_time})</small>"
        if self.tasks_done > 3:
            eta_s = (self.total_tasks - self.tasks_done) * (working_s / self.tasks_done)
            eta_time = time.strftime("%H:%M:%S", time.gmtime(eta_s))
            # Each task's own running time, from its gate slot to its return, so a pause that
            # held tasks at the gate is not in it; one a task spent waiting to retry is
            avg_per_op_s = self.cumulative_task_time / self.tasks_done
            time_msg += f""" | <small> Avg time per task: {avg_per_op_s:.2f}s
            | Max: {self.max_task_time:.2f}s</small>
            <br><code>ETA: {eta_time}</code>"""
        self._push(f"{task_progress_msg}{time_msg}", self.tasks_done, self.total_tasks)

    def update_preparation_progress(
        self,
        notes_prepared: int = 0,
        total_notes: int = 0,
        tasks_planned: int = 0,
    ):
        """Update the dialog while a nested op works out what it has to do.

        Nothing is running yet at this point, but an op that fans out per note has to read
        every note's word list before it knows its task total, and for a large selection that
        pass takes long enough to look like a hang if the dialog says nothing.
        """
        elapsed_s = time.time() - self.start_time
        elapsed_time = time.strftime("%H:%M:%S", time.gmtime(elapsed_s))
        task_progress_msg = f"""<strong>Preparing:</strong>
            <br><strong><code>{notes_prepared}/{total_notes}</code></strong> notes read
            <small style="opacity: 0.85"> | Tasks found: {tasks_planned}</small>
            <br><code>Time: {elapsed_time}</code>"""
        self._push(task_progress_msg, notes_prepared, total_notes)

    def update_note_adding_progress(
        self,
        notes_added: int = 0,
        total_notes: int = 0,
        failed: int = 0,
    ):
        """
        Update the Step 2 progress dialog for note adding operations occuring after async tasks
        are done.
        """
        self._adding_counts = (notes_added, total_notes, failed)
        try:
            label = self._note_adding_label(notes_added, total_notes, failed)
        except Exception as e:
            logger.error("Error updating note adding progress: %s", e)
            return
        self._push(label, notes_added, total_notes)

    def _note_adding_label(self, notes_added: int, total_notes: int, failed: int) -> str:
        failed_msg = ""
        if failed > 0:
            failed_msg = f""" | <strong style="color: red;">{failed} failed</strong>"""
        hint = ""
        if self._cleanup_cancel_armed.is_set():
            hint = (
                '<br><small style="opacity: 0.85">Cancel stops the adding; the words of'
                " notes not added are left to match again.</small>"
            )
        # A failed add took its time too, so the rate is over every note tried. The API
        # phase's notes_done, which this used to subtract, has nothing to do with adding.
        return self._note_stage_label(
            "Adding notes",
            notes_added,
            total_notes,
            notes_timed=notes_added + failed,
            after_count=failed_msg,
            after_time=hint,
        )

    def update_new_note_processing_progress(
        self,
        new_notes_processed: int = 0,
        total_notes: int = 0,
    ):
        """Update the Step 3 progress dialog for processing new notes after they have been added."""
        self._push_note_stage("Processing new notes", new_notes_processed, total_notes)

    def update_unadded_note_clearing_progress(
        self,
        notes_cleared: int = 0,
        total_notes: int = 0,
    ):
        """Progress of unlinking the new notes a cancel of the adding left out, the stage after
        the new-note processing."""
        self._push_note_stage("Unlinking notes not added", notes_cleared, total_notes)

    def update_marker_tidying_progress(self, words_done: int = 0, total_words: int = 0):
        """Progress of tidying the sort field markers, the cleanup's last stage."""
        self._push_note_stage("Tidying sort field markers", words_done, total_words, "word")

    def _push_note_stage(
        self, label: str, notes_done: int, total_notes: int, unit: str = "note"
    ) -> None:
        # Another stage's label is up, and end_cleanup_cancel has no hint to take off
        self._adding_counts = None
        self._push(
            self._note_stage_label(label, notes_done, total_notes, unit=unit),
            notes_done,
            total_notes,
        )

    def _note_stage_label(
        self,
        stage: str,
        notes_done: int,
        total_notes: int,
        *,
        notes_timed: Optional[int] = None,
        after_count: str = "",
        after_time: str = "",
        unit: str = "note",
    ) -> str:
        """The label of one of the cleanup's note stages: its count, time, average and ETA.

        `notes_timed` is how many notes the stage's time went on, when that is more than
        `notes_done` counts (the adding's failed notes); the average and the ETA are over it.
        `after_count` and `after_time` are HTML put after the count and after the timing.
        `unit` is what is counted, in the singular: the marker tidying counts words.
        """
        timed = notes_done if notes_timed is None else notes_timed
        elapsed_s = time.time() - self.start_time
        elapsed_time = time.strftime("%H:%M:%S", time.gmtime(elapsed_s))
        time_msg = f"<br><code>Time: {elapsed_time}</code>"
        if timed > 0:
            avg_per_note_s = elapsed_s / timed
            eta_s = (total_notes - timed) * avg_per_note_s
            eta_time = time.strftime("%H:%M:%S", time.gmtime(eta_s))
            time_msg += f""" | <small> Avg time per {unit}: {avg_per_note_s:.2f}s</small>
            <br><code>ETA: {eta_time}</code>"""
        count_msg = f"""<strong>{stage}:</strong>
            <br><strong><code>{notes_done}/{total_notes}</code></strong> {unit}s"""
        return f"{count_msg}{after_count}{time_msg}{after_time}"


def make_inner_bulk_op(
    config: dict,
    op: Callable[..., Union[bool, Coroutine[Any, Any, bool]]],
    gate: ConcurrencyGate,
    progress_updater: AsyncTaskProgressUpdater,
    handle_op_error: Callable[[Exception], None],
    handle_op_result: Callable[[bool], None],
    cancel_state: Optional[CancelState] = None,
    one_task_per_op: bool = False,
) -> Callable[..., Coroutine[Any, Any, bool]]:
    """
    Creates an asynchronous operation processor for bulk operations, limited by the shared
    concurrency gate and reporting progress as it goes.

    Requests are not paced here: each provider throttles itself against the rate-limit
    responses it gets back (see api_client.post_with_retry). What this limits instead is how
    many operations are in flight at once, which is what drives memory use.

    :param config (dict): Addon config
    :param op (Callable[[dict, ...], bool]): The operation function to execute for each item. It
            accepts the config dictionary as the first argument, followed by additional arguments.
    :param gate (ConcurrencyGate): Shared gate limiting concurrent operations. Must be the same
            instance for every task in a bulk run, otherwise nothing is actually limited.
    :param progress_updater (AsyncTaskProgressUpdater): Progress dialog updater.
    :param handle_op_error (Callable[[Exception], None]): Callback to handle exceptions raised during
            operation execution.
    :param handle_op_result (Callable[[bool], None]): Callback to handle the result of each operation.
    :param cancel_state (Optional[CancelState]): Shared cancellation state.
    :param one_task_per_op (bool): Whether each operation corresponds to a single task for progress
            tracking. If False, the caller is responsible for updating done note counts.

    Returns:
        Callable[..., Coroutine[Any, Any, bool]]: An asynchronous function that processes a
            single operation.
    """
    cancel_state = cancel_state or CancelState()

    # Wrapper function to process a single note
    async def process_op(
        notes_to_add_dict: dict[str, list[Note]],
        notes_to_update_dict: dict[NoteId, Note],
        **op_args,
    ) -> bool:
        """Process a single operation, waiting for a slot in the concurrency gate first.
        Args:
            notes_to_add_dict (dict[str, list[Note]]): Dictionary of notes to add.
            notes_to_update_dict (dict[NoteId, Note]): Dictionary of notes to update.
            **op_args: Additional keyword arguments to pass to the operation function.
        Returns:
            bool: The result of the operation, True if successful, False otherwise.
        """
        op_result = False
        try:
            # Wait until memory allows another operation to start
            await gate.acquire()
            try:
                # Check for cancel request before starting the operation
                if mw.progress.want_cancel():
                    logger.debug("Inner bulk operation mw.progress.want_cancel()")
                    return False

                progress_updater.increment_counts(
                    tasks_in_progress=1,
                )
                task_start_time = time.time()
                progress_updater.update_progress()

                try:
                    if mw.progress.want_cancel() or cancel_state.is_cancelled():
                        logger.debug("Inner process op: cancellation requested")
                        return False

                    # If the op itself is async, run it directly; otherwise run the blocking call
                    # in the thread pool so the event loop is not blocked by HTTP requests.
                    # to_thread uses the loop's default executor, which selected_notes_op has
                    # pointed at the run's shared, bounded pool.
                    called: Union[bool, Coroutine[Any, Any, bool]]
                    if asyncio.iscoroutinefunction(op):
                        called = op(
                            config,
                            notes_to_add_dict=notes_to_add_dict,
                            notes_to_update_dict=notes_to_update_dict,
                            **op_args,
                        )
                    else:
                        called = await asyncio.to_thread(
                            op,
                            config,
                            notes_to_add_dict=notes_to_add_dict,
                            notes_to_update_dict=notes_to_update_dict,
                            **op_args,
                        )

                    # A sync op that hands back a coroutine - a wrapper around an async
                    # op, which iscoroutinefunction does not see through - is awaited here.
                    op_result = await called if asyncio.iscoroutine(called) else called

                except RunCancelled as e:
                    # The op asked the collection for something after the run was cancelled.
                    # Expected, not an error: the task is being abandoned on purpose, and
                    # whatever it had done so far is simply left unfinished.
                    logger.log(diagnostic_level(), "Op abandoned on cancellation: %s", e)
                    return False
                except Exception as e:
                    logger.error("Inner process op error, passing to handle_op_error: %s", e)
                    handle_op_error(e)
                    return False
                finally:
                    task_time = time.time() - task_start_time
                    # A task that only finishes well after the cancel is one that kept working
                    # regardless of it; how long it took to stop is the thing to measure
                    since_cancel = seconds_since_cancel()
                    if since_cancel is not None:
                        logger.log(
                            diagnostic_level(),
                            "[stage] op returned %.1fs after the cancel (ran %.1fs)",
                            since_cancel,
                            task_time,
                        )
                    progress_updater.increment_counts(
                        tasks_done=1,
                        tasks_in_progress=-1,
                        cumulative_task_time=task_time,
                        notes_done=1 if one_task_per_op else 0,
                    )
                    progress_updater.update_progress()
            finally:
                gate.release()

            # Handle results
            handle_op_result(op_result)
            return op_result

        except asyncio.CancelledError:
            return False

    return process_op


class NotePlan(NamedTuple):
    """One note's work, worked out before any of it has been started.

    `task_count` is how many API tasks the note will produce. Knowing it before the run starts
    is what lets the progress dialog show the real total from the beginning, instead of a total
    that climbs every time a window of notes is reached.

    `spawn` creates those tasks, appending them to the window's task list. It is called only
    when the note's window comes up, so the tasks themselves - which each hold a note and a
    prompt for as long as they live - still exist only a window at a time.

    `flush`, for a note whose tasks save it together once they are all done: runs that save
    with what the finished tasks left, returning whether it saved the note - False when the
    finished tasks left nothing to save, or when it has run already. A cancel cancels the
    saving task along with the unfinished ones, which lost the finished ones' paid work;
    bulk_nested_notes_op flushes every started note once the driver returns. The save must run
    once only, whichever comes first (`run_once` builds both from one save), and must copy
    anything a worker thread the cancel abandoned can still be writing.
    """

    task_count: int
    spawn: Callable[[list[asyncio.Task]], None]
    flush: Optional[Callable[[], bool]] = None


def run_once(save: Callable[[], bool]) -> Callable[[], bool]:
    """`save` made to run once only, whichever of a note's own save and its `NotePlan.flush`
    comes first; a later call returns False, having saved nothing.

    `save` returns whether it saved the note. Both callers run on the op's event-loop thread
    (the note's saving task, then the flush after the driver returns), so a flag is enough.
    """
    done = False

    def once() -> bool:
        nonlocal done
        if done:
            return False
        done = True
        return save()

    return once


async def run_plans_rolling(
    plans: "Sequence[NotePlan]",
    gate: ConcurrencyGate,
    progress_updater: AsyncTaskProgressUpdater,
    cancel_state: CancelState,
    label: str,
    started_plans: "Optional[list[NotePlan]]" = None,
) -> bool:
    """Run every plan, keeping the task budget full instead of processing fixed windows.

    A task holds its note, and once it has a slot its prompt and its response, for as long as
    it lives, so only so many of them may exist at once. That budget is `gate.limit * TASK_QUEUE_DEPTH`, the same figure the window
    size used to be; what changed is that it is refilled as individual tasks finish rather
    than after a whole window has. Waiting for a whole window drained the gate from full to
    empty at every boundary - the slowest request in the window held `limit - 1` slots idle
    while it finished - and because the adapt loop only raises the limit while the gate is
    saturated, those idle stretches also cost the run the concurrency it was entitled to grow
    into. Recomputing the budget on every refill is what lets a raised limit take effect at
    once rather than at the next boundary.

    Nothing is a barrier any more. The first pass used to be one, because the memory estimator
    measured RSS growth across a window and needed that window to both start and end with
    nothing in flight to tell its own cost apart from memory the run had accumulated. The
    estimator now fits memory against the live task count instead, which separates the two
    without needing a moment of quiet - so the driver only has to keep reporting that count.

    Shared per-run state (word locks, generated meanings) lives in the caller's closure and is
    unaffected by which plans happen to be in flight together.

    Every plan whose spawn has run is appended to `started_plans`, if given: after a cancel
    those are the only notes that can have unsaved results, and a note never started must be
    left exactly as it was.

    Returns True if the run was cancelled.
    """
    # Only the live tasks, so a long run does not accumulate finished ones. The value is the
    # share of its plan's API task count that the task carries: a plan can spawn several tasks
    # while the budget is spent in API tasks, so splitting the plan's count evenly over them
    # lets the budget be repaid task by task as they finish.
    live: "dict[asyncio.Task, float]" = {}
    live_cost = 0.0
    index = 0
    cancelled = False

    done_queue: "asyncio.Queue[asyncio.Task]" = asyncio.Queue()
    cancel_manager = CancelManager(live, cancel_state, progress_updater=progress_updater)

    def fill() -> None:
        """Start plans until the budget is spent or there are none left."""
        nonlocal index, live_cost
        budget = max(1, gate.limit * TASK_QUEUE_DEPTH)
        # Always start at least one plan, however many tasks it turns out to want, so a note
        # costing more than the whole budget cannot stall the run
        while index < len(plans) and (not live or live_cost < budget):
            if mw.progress.want_cancel():
                return
            plan = plans[index]
            index += 1
            spawned: "list[asyncio.Task]" = []
            plan.spawn(spawned)
            if started_plans is not None:
                started_plans.append(plan)
            if not spawned:
                continue
            # A plan with no API tasks of its own still creates the bookkeeping tasks that
            # write its note back, so it cannot count as free: a run of them would never spend
            # the budget and every note would be started at once.
            cost = float(max(1, plan.task_count))
            share = cost / len(spawned)
            for task in spawned:
                live[task] = share
                task.add_done_callback(done_queue.put_nowait)
            live_cost += cost

    def stop_for_cancel() -> None:
        """Unwind the run: cancel whatever is live and let go of everything queued.

        Reached both from the driver noticing the cancel itself and from the monitor having
        noticed it first, so it has to cope with the teardown already having been done. Both
        halves matter while the run is rolling: unlike a window boundary, there are live tasks
        and tasks queued on the gate at every point in it.
        """
        if not cancel_manager.is_cancel_requested():
            cancel_manager.request_cancel()
        marker = time.monotonic()
        gate.abort()
        log_phase(f"{label}: gate.abort", marker)

    started = time.monotonic()
    try:
        while True:
            if mw.progress.want_cancel() or cancel_state.is_cancelled():
                logger.debug("%s: cancelled, returning results so far", label)
                stop_for_cancel()
                cancelled = True
                break

            fill()
            # The API tasks are alive from here, whether or not they hold a gate slot yet.
            # That count is what the estimator fits memory against - live and not in flight,
            # because concurrency.memory_per_slot divides the same TASK_QUEUE_DEPTH back out.
            # live_cost and not len(live): spawn() also creates per-word-list and per-note
            # bookkeeping tasks, which never touch the gate and would only dilute it.
            gate.note_live_tasks(live_cost)
            if not live:
                # Plans left over with nothing running means fill() stopped early, which it
                # only does for a cancel that arrived while it was starting them
                if index < len(plans):
                    logger.debug("%s: cancelled while starting tasks", label)
                    stop_for_cancel()
                    cancelled = True
                break

            try:
                finished = await wait_for_completions(done_queue, cancel_manager)
            except asyncio.CancelledError:
                # Someone cancelled the op's own task. Swallowed rather than propagated so the
                # run still gets to save the results it already has, as it does for a cancel
                # that comes in through the progress dialog.
                logger.debug("%s: cancelled while waiting for tasks", label)
                stop_for_cancel()
                cancelled = True
                break
            drain_task_errors(finished)
            for task in finished:
                live_cost -= live.pop(task, 0.0)
            live_cost = max(0.0, live_cost)

            if cancel_manager.is_cancel_requested():
                logger.debug("%s: cancelled, returning results so far", label)
                stop_for_cancel()
                cancelled = True
                break

            # Reported again now that the finished tasks have let their notes and prompts go,
            # so the fit sees the count come down as well as go up
            gate.note_live_tasks(live_cost)
    finally:
        cancel_manager.mark_work_complete()
        if not cancel_manager.monitor_task.done():
            cancel_manager.monitor_task.cancel()
        log_phase(
            f"{label}: run plans",
            started,
            plans=len(plans),
            started_plans=index,
            cancelled=cancelled,
            abandoned=sum(1 for task in live if not task.done()),
            threads=threading.active_count(),
        )
    return cancelled


def flush_started_plans(started_plans: "Sequence[NotePlan]", cancelled: bool) -> int:
    """Run the flush of every started plan that has one, returning how many notes it saved
    that their own tasks had not. A flush that found nothing to save counts as none.

    Each flush in its own try: one note's error must not keep the others' results from
    cleanup. A flush that raised counts as left unsaved.
    """
    unsaved = 0
    for plan in started_plans:
        if plan.flush is None:
            continue
        try:
            if plan.flush():
                unsaved += 1
        except Exception as e:
            unsaved += 1
            logger.error(f"Error saving a note's finished results: {e}")
            print_error_traceback(e, logger)
    if unsaved and cancelled:
        logger.info("Saved the finished results of %d notes the cancel left unsaved", unsaved)
    elif unsaved:
        # A finished run leaves one only when a task raised past process_op, so the gather in
        # the note's saving task did too and the save never ran; drain_task_errors logged why
        logger.error("%d notes were left unsaved by a run that was not cancelled", unsaved)
    return unsaved


async def bulk_nested_notes_op(
    message: str,
    config: dict,
    bulk_inner_op: Callable[..., Optional[NotePlan]],
    col: Collection,
    notes: Sequence[Note],
    edited_nids: list[NoteId],
    progress_updater: AsyncTaskProgressUpdater,
    notes_to_add_dict: dict[str, list[Note]],
    notes_to_update_dict: dict[NoteId, Note],
    notes_to_remove: Optional[list[NoteId]] = None,
    model: str = "",
    on_end: Optional[Callable[..., None]] = None,
) -> tuple[int, dict[str, list[Note]], dict[NoteId, Note], list[NoteId]]:
    """
    Perform a bulk operation on a sequence of notes, with multiple nested async operations occurring
    per note instead of just one. Otherwise similar to `bulk_notes_op` except this cannot be
    performed synchronously and thus, requires rate limits to be set in the config.


    :param message: A message to display in the progress dialog.
    :param config: Addon config dict.
    :param bulk_inner_op: The nested operation function to apply to each note. It is called once
           per note up front and must not start any work itself: it returns a NotePlan saying how
           many tasks the note will produce and how to create them, or None if the note has
           nothing to do. This op itself handles calling inner_bulk_op and updating updated_notes
           and edited_nids.
    :param col: The Anki collection object.
    :param notes: A sequence of Note objects to process.
    :param edited_nids: A list to store the IDs of edited notes, to be mutated in place.
    :param model: The AI model to use for the operation.
    :param on_end: An optional callback to run on completion of the bulk op. Should be running other
           side effects that do not edit or add notes as those should be handled through
    """
    pos = col.add_custom_undo_entry(f"{message} for {len(notes)} notes.")
    if notes_to_remove is None:
        notes_to_remove = []
    if not model:
        logger.error("Model arg missing in bulk_nested_notes_op, aborting")
        return pos, notes_to_add_dict, notes_to_update_dict, notes_to_remove

    progress_updater.set_total_notes(len(notes))

    # The message doubles as the op's identity for the learned per-task memory cost. The pool
    # is sized to the gate's ceiling, which is a guess until the op has been measured - so the
    # gate says when it raises it and the pool follows, rather than staying at the guess.
    gate = ConcurrencyGate(
        config,
        op_key=message,
        on_ceiling_changed=size_pools_to_ceiling,
        is_paused=run_paused,
    )
    progress_updater.gate = gate
    gate.start_adapting()
    size_pools_to_ceiling(gate.max_limit)
    rate_limit_tracker.reset()

    cancel_state = CancelState()

    # Work out what every note needs doing before starting any of it. This pass is synchronous
    # and creates no tasks - it only reads the notes, which are in memory already - so it costs
    # nothing in concurrency, and it is what gives the dialog the run's real task total from the
    # start. A note here can fan out to anywhere between one and dozens of API calls, so a count
    # of notes on its own says very little about how much work is left.
    plan_started = time.monotonic()
    plans: list[NotePlan] = []
    planned_tasks = 0
    for note_index, note in enumerate(notes):
        if mw.progress.want_cancel():
            logger.debug("Nested bulk op cancelled while planning")
            cancel_state.cancel()
            break
        plan = bulk_inner_op(
            config,
            note,
            edited_nids=edited_nids,
            notes_to_add_dict=notes_to_add_dict,
            notes_to_update_dict=notes_to_update_dict,
            progress_updater=progress_updater,
            cancel_state=cancel_state,
            gate=gate,
        )
        if plan is not None:
            plans.append(plan)
            planned_tasks += plan.task_count
        progress_updater.set_total_tasks(planned_tasks)
        progress_updater.update_preparation_progress(
            notes_prepared=note_index + 1,
            total_notes=len(notes),
            tasks_planned=planned_tasks,
        )
    log_phase("nested op: plan notes", plan_started, notes=len(notes), tasks=planned_tasks)

    # Only now that the total is known: until this point the periodic updater would be drawing
    # a task line whose total is still growing, over the preparation line
    progress_updater.start_autoupdate()

    # And only now can the gate decide whether measuring this run is worth what it costs - it
    # needs the task count, and the pass above is exactly the allocation-heavy stretch that
    # would have been traced for nothing had measuring started with the adapt loop.
    gate.begin_measuring(planned_tasks)

    started_plans: list[NotePlan] = []
    try:
        # Every task holds onto its note, prompt and config for as long as it lives, so they
        # are not all created up front: a plan is only the closure that will create one when
        # the rolling driver has room for it.
        cancelled = await run_plans_rolling(
            plans,
            gate=gate,
            progress_updater=progress_updater,
            cancel_state=cancel_state,
            label="nested op",
            started_plans=started_plans,
        )
        if cancelled:
            logger.debug("Bulk operation was cancelled, returning results so far")
    finally:
        marker = time.monotonic()
        gate.finish()
        marker = log_phase("nested op: gate.finish", marker)
        progress_updater.gate = None

    # The notes to add are the ones registered by now, taken before the flush: a thread a
    # cancel abandoned goes on registering new notes, and one registered after the flush has
    # its placeholder in no result saved, so added it would be a note no sentence links to,
    # and its word matched again for a duplicate. A note is registered before its placeholder
    # goes into its result, so every one taken here is linked once flushed; one registered
    # during the flush can leave a placeholder of a note never added, which the next match
    # run resets. Deep: the thread appends to the word's list, not just the dict.
    registered = {word: list(word_notes) for word, word_notes in list(notes_to_add_dict.items())}
    # Only once the driver has returned: until then a note's own save may still come. Outside
    # the finally, as a run the driver raised out of fails before cleanup and keeps nothing
    # anyway. Before on_end, which is for side effects that edit no notes and so may expect
    # the op's results to be complete.
    flush_started_plans(started_plans, cancelled)
    marker = log_phase("nested op: flush", marker)

    if on_end:
        on_end()
        marker = log_phase("nested op: on_end", marker)
    progress_updater.stop_autoupdate()
    log_phase("nested op: stop_autoupdate", marker, threads=threading.active_count())
    return pos, registered, notes_to_update_dict, notes_to_remove


def sync_bulk_notes_op(
    pos: int,
    col: Collection,
    config: dict,
    op: Callable[..., bool],
    notes: Sequence[Note],
    edited_nids: list[NoteId],
    message: str,
    notes_to_add_dict: Optional[dict[str, list[Note]]] = None,
    notes_to_update_dict: Optional[dict[NoteId, Note]] = None,
    notes_to_remove: Optional[list[NoteId]] = None,
    on_end: Optional[Callable[..., None]] = None,
):
    """
    Perform a simple sync bulk operation on a sequence of notes. Will run the operation
    function on each note, updating the progress dialog and collecting edited note IDs.

    Used as a fallback for when the async version is not needed or rate limits are not set.

    :param pos: The position in the undo stack to add the operation.
    :param col: The Anki collection object.
    :param config: Addon config dict.
    :param op: The operation function to apply to each note.
    :param col: The Anki collection object.
    :param notes: A sequence of Note objects to process.
    :param edited_nids: A list to store the IDs of edited notes, to be mutated in place.
    :param message: A message to display in the progress dialog.
    :param on_end: An optional callback to run on completion of the bulk op. Should be running other
            side effects that do not edit or add notes as those should be handled through
            notes_to_add_dict and notes_to_update_dict.
    """
    total_notes = len(notes)
    if notes_to_remove is None:
        notes_to_remove = []
    note_cnt = 0
    start_time = time.time()
    # Left out of the ETA: the notes still to do will not spend it again
    paused_s = 0.0
    dialog_cancel = DialogCancelState()
    for note in notes:
        # Checked before a note rather than after one, so a pause that comes after the last
        # note holds nothing up
        if run_paused():
            pause = pause_state()
            pause_msg = format_pause_line(pause) if pause else "<b>Paused</b>"
            # Nothing else redraws the dialog while this thread waits, so say why it stopped
            mw.taskman.run_on_main(
                partial(
                    show_dialog_label, f"<b>{message}</b><br>{pause_msg}", note_cnt, total_notes
                )
            )
            paused_at = time.time()
            if not wait_while_paused(dialog_cancel):
                break
            paused_s += time.time() - paused_at
        try:
            op(
                config=config,
                note=note,
                notes_to_add_dict=notes_to_add_dict,
                notes_to_update_dict=notes_to_update_dict,
            )
        except Exception as e:
            logger.error("Sync bulk notes op: Error processing note %s: %s", note.id, e)
        note_cnt += 1

        elapsed_s = time.time() - start_time
        elapsed_time = time.strftime("%H:%M:%S", time.gmtime(elapsed_s))
        time_msg = f"<br><code>Time: {elapsed_time}</code>"
        if note_cnt > 3:
            eta_s = (total_notes - note_cnt) * ((elapsed_s - paused_s) / note_cnt)
            eta_time = time.strftime("%H:%M:%S", time.gmtime(eta_s))
            time_msg += f"""<br><code>ETA: {eta_time}</code>"""
        mw.taskman.run_on_main(
            partial(
                show_dialog_label,
                f"<b>{message}</b><br>{note_cnt}/{total_notes} notes processed{time_msg}",
                note_cnt,
                total_notes,
            )
        )
        if mw.progress.want_cancel():
            break

    if on_end:
        on_end()

    # The dialog is not closed here, nor in on_bulk_success: aqt's with_progress finishes the
    # progress once the whole operation is over (a chain's own level keeps the window up
    # until its last step). Closing it here left the note-adding phase of the cleanup drawing
    # progress into a window that was already gone, and as a phase of a multi-phase op it
    # would have taken the cancel button away from every phase after this one.

    return pos, notes_to_add_dict, notes_to_update_dict, notes_to_remove


BulkOpResult = tuple[int, dict[str, list[Note]], dict[NoteId, Note], list[NoteId]]


async def bulk_notes_op(
    message,
    config,
    op,
    col: Collection,
    notes: Sequence[Note],
    edited_nids: list[NoteId],
    progress_updater: AsyncTaskProgressUpdater,
    notes_to_add_dict: Optional[dict[str, list[Note]]] = None,
    notes_to_update_dict: Optional[dict[NoteId, Note]] = None,
    notes_to_remove: Optional[list[NoteId]] = None,
    on_end: Optional[Callable[..., None]] = None,
    is_sync_op: bool = False,
) -> BulkOpResult:
    """
    Perform a simple async or sync bulk operation on a sequence of notes. Will run the operation
    function on each note, updating the progress dialog and collecting edited note IDs.
    Each note will create one async task.

    The bulk op runs asynchronously unless is_sync_op is set. How many notes are processed at
    once is decided by the shared ConcurrencyGate, based on available memory, not by a
    configured request rate — the providers throttle themselves against their own rate-limit
    responses.

    Args:
        message: A message to display in the progress dialog.
        config: Addon config dict.
        op: The operation function to apply to each note.
        col: The Anki collection object.
        notes: A sequence of Note objects to process.
        edited_nids: A list to store the IDs of edited notes, to be mutated in place.
        is_sync_op: Run the notes sequentially instead of concurrently. For local ops that
            make no API calls.
        on_end: An optional callback to run on completion of the bulk op. Should be running other
            side effects that do not edit or add notes as those should be handled through
            notes_to_add_dict and notes_to_update_dict.
    """
    if notes_to_remove is None:
        notes_to_remove = []
    if notes_to_add_dict is None:
        notes_to_add_dict = {}
    if notes_to_update_dict is None:
        notes_to_update_dict = {}
    pos = col.add_custom_undo_entry(f"{message} for {len(notes)} notes.")
    if is_sync_op:
        return sync_bulk_notes_op(
            pos=pos,
            col=col,
            config=config,
            op=op,
            notes=notes,
            edited_nids=edited_nids,
            message=message,
            notes_to_add_dict=notes_to_add_dict,
            notes_to_update_dict=notes_to_update_dict,
            notes_to_remove=notes_to_remove,
            on_end=on_end,
        )

    updated_notes: list[Note] = []

    progress_updater.set_total_notes(len(notes))
    progress_updater.set_total_tasks(len(notes))
    # Can start auto updater now that we're in an async context with a running loop
    progress_updater.start_autoupdate()

    # The message doubles as the op's identity for the learned per-task memory cost. The pool
    # is sized to the gate's ceiling, which is a guess until the op has been measured - so the
    # gate says when it raises it and the pool follows, rather than staying at the guess.
    gate = ConcurrencyGate(
        config,
        op_key=message,
        on_ceiling_changed=size_pools_to_ceiling,
        is_paused=run_paused,
    )
    progress_updater.gate = gate
    gate.start_adapting()
    # One task per note here, and no planning pass to wait for, so the run's size is already
    # known - but the decision is the same one the nested op makes after planning.
    gate.begin_measuring(len(notes))
    size_pools_to_ceiling(gate.max_limit)
    rate_limit_tracker.reset()

    def handle_op_success(
        note: Note,
        was_success: bool,
    ):
        """Handle successful operation result."""
        if was_success and edited_nids is not None:
            updated_notes.append(note)
            edited_nids.append(note.id)
        logger.debug(f"Bulk notes op success for note {note.id}, was_success: {was_success}")

    cancel_state = CancelState()
    cancelled = False

    try:
        # Every task holds onto its note for as long as it lives, and its prompt and response
        # from the moment it has a gate slot, so they are not all created up front: a plan is
        # only the closure that will create one when the rolling driver has room for it.
        def make_plan(note: Note) -> NotePlan:
            def handle_op_error(e: Exception) -> None:
                logger.error(f"Error during operation with note {note.id}: {e}")
                print_error_traceback(e, logger)

            def handle_op_result(was_success: bool) -> None:
                handle_op_success(note, was_success)

            def spawn(tasks: "list[asyncio.Task]") -> None:
                process_note = make_inner_bulk_op(
                    config=config,
                    op=op,
                    gate=gate,
                    progress_updater=progress_updater,
                    handle_op_error=handle_op_error,
                    handle_op_result=handle_op_result,
                    cancel_state=cancel_state,
                    one_task_per_op=True,
                )
                tasks.append(
                    asyncio.create_task(
                        process_note(
                            notes_to_add_dict=notes_to_add_dict,
                            notes_to_update_dict=notes_to_update_dict,
                            # note is passed to the op function, along with config in
                            # make_inner_bulk_op
                            note=note,
                        )
                    )
                )

            return NotePlan(task_count=1, spawn=spawn)

        cancelled = await run_plans_rolling(
            [make_plan(note) for note in notes],
            gate=gate,
            progress_updater=progress_updater,
            cancel_state=cancel_state,
            label="bulk op",
        )
    finally:
        marker = time.monotonic()
        gate.finish()
        marker = log_phase("bulk op: gate.finish", marker)
        progress_updater.gate = None

    if not cancelled:
        logger.debug("Bulk notes op completed successfully, updating notes")

    if on_end:
        on_end()
        marker = log_phase("bulk op: on_end", marker)

    progress_updater.stop_autoupdate()
    log_phase(
        "bulk op: stop_autoupdate",
        marker,
        cancelled=cancelled,
        to_update=len(notes_to_update_dict),
        to_add=sum(len(v) for v in notes_to_add_dict.values()),
        threads=threading.active_count(),
    )
    return pos, notes_to_add_dict, notes_to_update_dict, notes_to_remove


class OpPhase(NamedTuple):
    """One phase of a multi-phase op: a whole bulk op run over the whole selection.

    `bulk_op` is exactly what `selected_notes_op` takes on its own - a coroutine function
    wrapping `bulk_notes_op` or `bulk_nested_notes_op` - so an op that already has a menu
    entry of its own becomes a phase without changing anything about it. `name` is what the
    progress dialog shows beside "Phase 1/2".
    """

    name: str
    bulk_op: Callable[..., Coroutine[Any, Any, Optional[BulkOpResult]]]


async def run_op_phases(
    phases: Sequence[OpPhase],
    col: Collection,
    notes: Sequence[Note],
    edited_nids: list[NoteId],
    progress_updater: AsyncTaskProgressUpdater,
    notes_to_add_dict: dict[str, list[Note]],
    notes_to_update_dict: dict[NoteId, Note],
    label: str = "",
) -> BulkOpResult:
    """Run every phase over the same notes, one after another, as a single operation.

    The same thing as running the phases' menu entries by hand, in one click. A phase has to
    be a whole bulk op rather than another step inside one, because a nested phase cannot be
    planned until the earlier phase's API calls are done: `bulk_nested_notes_op` fixes every
    note's `NotePlan.task_count` before it starts anything, and the judge's words only exist
    once the word array has been generated.

    What the phases share is `selected_notes_op`'s run: one list of notes, so a later phase
    reads the earlier phases' writes straight from memory and each note is still written to
    the collection once, in cleanup; one pair of add/update dicts; and one undo entry, since
    cleanup merges everything into the first phase's `pos`.

    What they do not share is added notes. A note a phase adds only gets an id in cleanup,
    after every phase has run, so a phase cannot work on notes an earlier phase created -
    that still needs the ops to be run one after another from the menu.

    Cancelling is checked between phases as well as inside them: a cancelled phase returns
    what it managed to do, and the run then stops rather than starting the next one.
    """
    pos: Optional[int] = None
    notes_to_remove: list[NoteId] = []
    # The phases' answers, not the shared dict: a phase answers with the notes it wants added,
    # which for bulk_nested_notes_op are those registered before its flush, while threads a
    # cancel abandoned may still be registering more in the shared dict. Starting from what it
    # was handed, and by identity under each key, as a phase answering with the shared dict
    # repeats the earlier phases' notes.
    to_add: dict[str, list[Note]] = {}
    to_add_ids: set[tuple[str, int]] = set()

    def fold_in(add_dict: dict[str, list[Note]]) -> None:
        for key, added in list(add_dict.items()):
            for note in list(added):
                if (key, id(note)) not in to_add_ids:
                    to_add_ids.add((key, id(note)))
                    to_add.setdefault(key, []).append(note)

    fold_in(notes_to_add_dict)
    total = len(phases)
    for index, phase in enumerate(phases):
        if index > 0 and (mw.progress.want_cancel() or run_cancelled()):
            logger.debug("Multi-phase op cancelled before phase %d/%d", index + 1, total)
            break
        # A paused run starts no new phase. The gate and the sync op would each hold the phase
        # at its first task anyway, but only after it had reset the dialog's counters and
        # clock and run its planning pass, so the pause would count against the phase's ETA.
        # Blocking the loop is fine between phases: the phase before has finished, and the
        # progress ticker and cancel monitor it ran on the loop have stopped with it.
        if index > 0 and run_paused():
            # Nothing else redraws the dialog during this wait, the ticker included
            next_phase = f"Phase {index + 1}/{total}"
            next_phase += f" ({phase.name})" if phase.name else ""
            progress_updater.show_paused(f"{next_phase} starts when the run resumes")
        if index > 0 and not wait_while_paused(DialogCancelState()):
            logger.debug("Multi-phase op cancelled while paused before phase %d", index + 1)
            break
        if total > 1:
            progress_updater.begin_phase(index + 1, total, phase.name)
        started = time.monotonic()
        result = await phase.bulk_op(
            col,
            notes=notes,
            edited_nids=edited_nids,
            progress_updater=progress_updater,
            notes_to_add_dict=notes_to_add_dict,
            notes_to_update_dict=notes_to_update_dict,
        )
        log_phase(f"phase {index + 1}/{total}: {phase.name}", started)
        if result is None:
            # An op that bailed out before starting, such as one that found no config
            logger.error("Phase %s returned no result, continuing with the next", phase.name)
            continue
        phase_pos, phase_add_dict, phase_update_dict, phase_notes_to_remove = result
        if pos is None:
            pos = phase_pos
        fold_in(phase_add_dict)
        # A phase is handed the shared dicts and normally returns those same objects, but it
        # is free to build its own, so fold anything new in rather than assuming identity.
        if phase_update_dict is not notes_to_update_dict:
            notes_to_update_dict.update(phase_update_dict)
        if phase_notes_to_remove:
            notes_to_remove.extend(phase_notes_to_remove)
    if pos is None:
        # Every phase bailed out. There is nothing to merge, but the cleanup still needs an
        # undo entry to merge its own writes into.
        pos = col.add_custom_undo_entry(label or "Multi-phase op")
    return pos, to_add, notes_to_update_dict, notes_to_remove


class NewNotesCounts(NamedTuple):
    """How a run's new notes fared in the cleanup, for the final message."""

    added: int = 0
    # Attempted and raised, or skipped with no note type or deck. The arrays keep their
    # placeholders until the next match run puts them back to be matched: a short-lived
    # hint for debugging, not a record to rely on.
    failed: int = 0
    # Never attempted, because the adding was cancelled first. Their words were unlinked.
    not_added: int = 0

    @property
    def prepared(self) -> int:
        return self.added + self.failed + self.not_added


def _count_new_notes(count: int) -> str:
    return "1 new note" if count == 1 else f"{count} new notes"


def new_notes_message(counts: NewNotesCounts) -> str:
    """The final message's lines about the new notes, "" when the run prepared none.

    Said after a cancel too, which adds them: they hold the paid-for work. A cancel of the
    adding itself is said as well, so the user sees what the second press did.
    """
    if not counts.prepared:
        return ""
    if counts.added == counts.prepared:
        message = f"<br>Added {_count_new_notes(counts.added)}."
    else:
        message = f"<br>Added {counts.added} of {_count_new_notes(counts.prepared)}."
    if counts.not_added:
        message += (
            f"<br>{counts.not_added} not added, as the adding was cancelled: the words linked"
            " to them are left to be matched again."
        )
    if counts.failed:
        message += (
            f"<br>{counts.failed} could not be added: the words linked to them keep their"
            " placeholder ids."
        )
    return message


def on_bulk_success(
    out,
    done_text: str,
    edited_nids: Sequence[NoteId],
    edited_other_nids: Sequence[NoteId],
    nids: Sequence[NoteId],
    parent: Browser,
    extra_callback=None,
    new_notes: NewNotesCounts = NewNotesCounts(),
    chain: Optional[ChainStep] = None,
    cancelled: bool = False,
):
    """End a run that returned: say how it went. aqt has finished the progress by now.

    From the menu that is a tooltip, or a warning for a run that stopped itself. As a step of
    a chain nothing is shown - the chain sums its steps up once it is over - and the message
    goes to `chain.on_done` instead. Its status is `stopped` for a stop reason, else
    `cancelled` when `selected_notes_op` saw the run cancelled on the op thread.
    """
    logger.debug("[phase] on_bulk_success reached")
    # No mw.progress.finish() here: aqt's with_progress finished the progress before calling
    # this. A second one ended whichever progress was open by then, which in a chain is the
    # chain's own, held across its steps, and closed its dialog between two steps.
    if chain is not None:
        try:
            message, stop_reason = bulk_success_message(
                done_text, edited_nids, edited_other_nids, nids, extra_callback, new_notes
            )
            if stop_reason:
                status = STEP_STOPPED
            elif cancelled:
                status = STEP_CANCELLED
            else:
                status = STEP_COMPLETED
            outcome = StepOutcome(status, message, stop_reason=stop_reason)
        except Exception as e:
            # Raised here, it would reach Qt's handler and the chain would wait forever
            outcome = failed_step_outcome(parent, e, chain.title)
        chain.on_done(outcome)
        return
    message, stop_reason = bulk_success_message(
        done_text, edited_nids, edited_other_nids, nids, extra_callback, new_notes
    )
    if stop_reason:
        # A tooltip would be gone before the user looks: the rest of the notes were not done
        message += f"<br><br><b>Stopped early.</b> {html.escape(stop_reason)}"
        showWarning(message, parent=parent, textFormat="rich")
        return
    tooltip(
        message,
        parent=parent,
        period=5000,
    )


def bulk_success_message(
    done_text: str,
    edited_nids: Sequence[NoteId],
    edited_other_nids: Sequence[NoteId],
    nids: Sequence[NoteId],
    extra_callback=None,
    new_notes: NewNotesCounts = NewNotesCounts(),
) -> tuple[str, Optional[str]]:
    """Run `extra_callback`, then return the end message and the stop reason, which it takes.

    One message for a run from the menu and for a step of a chain alike. The stop reason is
    not in the message: each of the two words it in its own way.
    """
    success_started = time.monotonic()
    if extra_callback:
        extra_callback()
        log_phase("success: extra_callback", success_started)
    message = f"{done_text} in {len(edited_nids)}/{len(nids)} selected notes."
    if edited_other_nids:
        message += f"<br>Edited {len(edited_other_nids)} other notes not among the selection."
    message += new_notes_message(new_notes)
    return message, take_stop_reason()


NewNotesOp = Callable[[list[Note], dict, AsyncTaskProgressUpdater], dict[NoteId, Note]]
FilterNewNotesOp = Callable[
    [list[Note], dict, AsyncTaskProgressUpdater],
    tuple[list[Note], dict[NoteId, Note]],
]


class NewNotesAdded(NamedTuple):
    """What `add_new_notes` did, for the cleanup to fold into its own state."""

    counts: NewNotesCounts
    # The last merge into the run's undo entry; None when nothing was written
    op_changes: Optional[OpChanges]
    # Notes new_notes_op or unadded_notes_op rewrote that were then saved, bar the added notes
    # themselves (their own id written in), which the counts have as added
    updated_nids: list[NoteId]
    # Notes filter_new_notes_op rewrote that were then saved
    filtered_nids: list[NoteId]
    # The notes added, and the other notes saved, for the marker tidying to look at
    added_notes: Sequence[Note] = ()
    saved_notes: Sequence[Note] = ()

    @property
    def added(self) -> int:
        return self.counts.added


def count_new_notes_edits(
    added: NewNotesAdded,
    selected: Container[NoteId],
    edited_nids: list[NoteId],
    edited_other_nids: list[NoteId],
) -> None:
    """Count the notes the note adding saved into the final message's two figures, each once:
    a selected note as one of the selection edited, any other as an other note.

    The resolving and the unlinking rewrite whichever sentences link to the new notes, often
    mostly outside the selection. All were once counted as selected, the added notes
    included, which read "in 50/10 selected notes" and left the other-notes line short.
    """
    count_edits(
        [*added.filtered_nids, *added.updated_nids], selected, edited_nids, edited_other_nids
    )


def count_edits(
    nids: Iterable[NoteId],
    selected: Container[NoteId],
    edited_nids: list[NoteId],
    edited_other_nids: list[NoteId],
) -> None:
    """Count notes the cleanup saved into the final message's two figures, each once.

    Against sets of what is counted already: the lists run to thousands of notes when a run's
    new notes are linked from thousands of sentences, and a list's `in` made that quadratic.
    """
    counted = set(edited_nids)
    counted_other = set(edited_other_nids)
    for nid in nids:
        if nid in selected:
            if nid not in counted:
                counted.add(nid)
                edited_nids.append(nid)
        elif nid not in counted_other:
            counted_other.add(nid)
            edited_other_nids.append(nid)


def _insert_deck_id(col: Collection, config: dict, note: Note) -> Optional[DeckId]:
    """The deck a new note goes into, or None when it cannot be added."""
    note_type = note.note_type()
    if note_type is None:
        logger.debug(f"Error: Note type for note {note.id} is None, skipping note adding")
        return None
    model_config = config.get(note_type["name"])
    # Optional, unlike the fields: get_field_config raises for a key left out, which stopped
    # the adding of every note in the run. A note type not configured at all still raises.
    insert_deck = (
        model_config.get("insert_deck")
        if isinstance(model_config, dict)
        else get_field_config(config, "insert_deck", note_type)
    )
    if insert_deck:
        insert_deck_id = col.decks.id_for_name(insert_deck)
    else:
        insert_deck_id = col.decks.id_for_name("Default")
        logger.debug("No insert deck set, setting deck_id to Default")
    if insert_deck_id is None:
        logger.debug("Default deck not found, skipping note adding")
    return insert_deck_id


def add_new_notes(
    col: Collection,
    notes_to_add: Sequence[Note],
    config: dict,
    pos: int,
    progress_updater: AsyncTaskProgressUpdater,
    new_notes_op: Optional[NewNotesOp] = None,
    *,
    filter_new_notes_op: Optional[FilterNewNotesOp] = None,
    unadded_notes_op: Optional[NewNotesOp] = None,
    notes_to_remove: Optional[set[NoteId]] = None,
) -> NewNotesAdded:
    """Add a run's new notes to the collection: dedupe them, add them, resolve their ids.

    The cleanup's note adding, finished and cancelled runs alike. Every write is merged into
    the run's undo entry `pos` as it is made, so the whole run stays one undo step.

    The adding has a cancel of its own (`arm_cleanup_cancel` re-arms the dialog's, here, when
    there is anything to add), honoured before the dedupe, between its merges, right after it,
    and before each note; never inside one `add_note`, whose hooks run to the end. Once it is
    seen the rest is not added, and the notes are split three ways:

    - added: `new_notes_op` resolves their placeholder ids, as in a finished run;
    - failed (attempted and raised, or no note type or deck): handed to neither op, so their
      placeholders stay in the arrays, a hint for debugging that lasts only until the next
      match run resets them (match_targets.resolve_placeholder_ids);
    - not added (never attempted): `unadded_notes_op` puts the words linked to them back to
      be matched again, since no note will ever hold their placeholders.

    Which notes are "not added" depends on where the cancel was seen. Before the first add
    (before, during or right after the dedupe) it is every note as prepared, the dedupe's
    duplicates included: an interrupted dedupe has remapped only some references of a
    duplicate it dropped, and the rest still point at its placeholder. During the adding it
    is the rest of the deduped list only. The dedupe finished then, so every reference to a
    duplicate points at the note kept in its place, which is in that list or already added.

    Resolving and unlinking always run to the end: they are what leaves the arrays
    consistent. A resolving that raises fails the op, but only after the unlinking has run.
    """
    total_notes = len(notes_to_add)
    logger.debug(f"Adding {total_notes} new notes to note_will_be_added hooks will be run")
    removed = notes_to_remove or set()
    # As prepared, before the dedupe drops any: see "not added" above
    prepared = list(notes_to_add)
    notes = prepared
    added_notes: list[Note] = []
    failed_cnt = 0
    not_added: list[Note] = []
    op_changes: Optional[OpChanges] = None
    updated_nids: list[NoteId] = []
    filtered_nids: list[NoteId] = []
    saved_notes: list[Note] = []
    started = time.monotonic()

    try:
        if notes:
            # First, so the label drawn as Cancel comes back, and the dedupe's after it, show
            # this stage's time, not the API phase's: one stage, whose arming takes moments
            progress_updater.begin_cleanup_stage()
            progress_updater.arm_cleanup_cancel(total_notes)
        cancelled = progress_updater.cleanup_cancel_requested()
        if filter_new_notes_op is not None and notes and not cancelled:
            notes, filtered_notes_to_update_dict = filter_new_notes_op(
                list(notes), config, progress_updater
            )
            # Saved even when the dedupe was cancelled: the references it remapped point at the
            # notes kept, which are prepared notes like the duplicates, and are unlinked alike
            valid_filtered_notes = [
                note
                for note in filtered_notes_to_update_dict.values()
                if note.id != 0 and note.id not in removed
            ]
            if valid_filtered_notes:
                try:
                    col.update_notes(valid_filtered_notes)
                except Exception as e:
                    logger.error(f"Error updating notes after filter_new_notes_op: {e}")
                    print_error_traceback(e, logger)
                op_changes = col.merge_undo_entries(pos)
                filtered_nids = [note.id for note in valid_filtered_notes]
                saved_notes.extend(valid_filtered_notes)
            started = log_phase("cleanup: filter_new_notes_op", started, kept=len(notes))
            cancelled = progress_updater.cleanup_cancel_requested()
        if cancelled:
            not_added = prepared
        else:
            total_notes = len(notes)
            progress_updater.begin_cleanup_stage()
            # Drawn before the first note too: its hooks can take a while, and until something
            # draws the dialog shows the stage before, and not that Cancel now stops the adding
            progress_updater.update_note_adding_progress(total_notes=total_notes)
            # The adding phase is a phase, not `total_notes` independent events, so it gets
            # one log file for the whole of itself. `note_will_be_added` fires inside this
            # block once per note, and the hook behind it used to open a file and close the
            # previous one every time: one measured run left 1,453 of them, and the run's
            # own log - the phase table included - ended up in the last. The handler goes
            # back the way it was at the end of the block, so everything either side of the
            # loop stays in one file.
            with phase_log("add_note_phase"):
                for index, note in enumerate(notes):
                    if progress_updater.cleanup_cancel_requested():
                        not_added = notes[index:]
                        break
                    insert_deck_id = _insert_deck_id(col, config, note)
                    if insert_deck_id is None:
                        failed_cnt += 1
                    else:
                        try:
                            logger.debug(f"Adding note {index} to deck {insert_deck_id}")
                            col.add_note(note, insert_deck_id)
                        except Exception as e:
                            logger.error(f"Error adding note {index}: {e}")
                            print_error_traceback(e, logger)
                            failed_cnt += 1
                        else:
                            added_notes.append(note)
                            op_changes = col.merge_undo_entries(pos)

                    progress_updater.update_note_adding_progress(
                        notes_added=len(added_notes),
                        total_notes=total_notes,
                        failed=failed_cnt,
                    )
            started = log_phase(
                "cleanup: add_note loop",
                started,
                added=len(added_notes),
                failed=failed_cnt,
                not_added=len(not_added),
            )
    finally:
        # However the adding ends, a raise included (a note type the config lacks, a failed
        # merge): the op still has its teardown ahead with the dialog up, and a Cancel left
        # live and armed there would say it can be stopped. Around the arming too, so a
        # re-arm still queued when it raised does nothing once it lands.
        progress_updater.end_cleanup_cancel()
    counts = NewNotesCounts(len(added_notes), failed_cnt, len(not_added))
    if not_added:
        logger.info(
            f"The adding was cancelled: {len(not_added)} of {counts.prepared} new notes not added"
        )

    # The added notes are counted as added; updated_nids holds the notes edited for them
    added_nids = {note.id for note in added_notes}
    resolving_error: Optional[Exception] = None
    if new_notes_op and added_notes:
        progress_updater.begin_cleanup_stage()
        additional_updates_notes_dict: dict[NoteId, Note] = {}
        try:
            # col.add_note mutates the note given, adding the id to it
            additional_updates_notes_dict = new_notes_op(
                list(added_notes), config, progress_updater
            )
        except Exception as e:
            # Raised again once the unlinking has run: the words of notes that will never
            # exist must not be left linked to them. Nothing the op rewrote in memory is
            # saved: the arrays and the added notes' id fields keep the placeholders, and the
            # next match run over those sentences resolves each as a placeholder one note
            # holds. Still a failed op, as it always was: the user has to see it.
            logger.error(f"Error resolving the new notes' ids: {e}")
            print_error_traceback(e, logger)
            resolving_error = e
        started = log_phase("cleanup: new_notes_op", started)

        # Every note the op is given has an id, and so has every note it rewrites; a failed
        # add is given to no op, and keeps no record but its placeholder
        valid_notes = list(additional_updates_notes_dict.values())
        if valid_notes:
            try:
                col.update_notes(valid_notes)
            except Exception as e:
                logger.error(f"Error updating valid notes after new_notes_op: {e}")
                print_error_traceback(e, logger)
            op_changes = col.merge_undo_entries(pos)
            updated_nids = [note.id for note in valid_notes if note.id not in added_nids]
            saved_notes.extend(valid_notes)

    if unadded_notes_op and not_added:
        # After the resolving has saved its notes: the unlinking reads the arrays from the
        # collection, and an added note's own array may link to a note that was not added
        progress_updater.begin_cleanup_stage()
        unlinked_notes_dict: dict[NoteId, Note] = {}
        try:
            unlinked_notes_dict = unadded_notes_op(list(not_added), config, progress_updater)
        except Exception as e:
            # Such as a note type missing from the config. The placeholders it leaves are
            # repaired by the next match run, which finds no note holding them.
            logger.error(f"Error unlinking the new notes not added: {e}")
            print_error_traceback(e, logger)
        unlinked_notes = [
            note
            for note in unlinked_notes_dict.values()
            if note.id > 0 and note.id not in removed
        ]
        if unlinked_notes:
            try:
                col.update_notes(unlinked_notes)
            except Exception as e:
                logger.error(f"Error updating notes after unadded_notes_op: {e}")
                print_error_traceback(e, logger)
            op_changes = col.merge_undo_entries(pos)
            already = {*updated_nids, *added_nids}
            updated_nids.extend(note.id for note in unlinked_notes if note.id not in already)
            saved_notes.extend(unlinked_notes)
        log_phase("cleanup: unadded_notes_op", started, unlinked=len(unlinked_notes))
    if resolving_error is not None:
        raise resolving_error
    return NewNotesAdded(
        counts, op_changes, updated_nids, filtered_nids, list(added_notes), saved_notes
    )


def tidy_markers(
    col: Collection,
    notes: Sequence[Note],
    config: dict,
    pos: int,
    progress_updater: AsyncTaskProgressUpdater,
    tidy_markers_op: NewNotesOp,
    notes_to_remove: Optional[set[NoteId]] = None,
) -> tuple[Optional[OpChanges], list[NoteId]]:
    """The cleanup's last stage: `tidy_markers_op` renames what the run left of the sort field
    markers of `notes`' words, and the notes it renames are saved into the run's undo entry.

    After everything else is saved, whether the run was cancelled or not and whether or not it
    had notes to add: a new reading whose meaning could not be made adds nothing and still
    leaves its markers. It cannot be cancelled, and a raise is logged and saves nothing: the
    markers are only names, and the run's work is saved already. Returns the undo entry's
    changes when something was saved, and the ids of the notes saved.
    """
    removed = notes_to_remove or set()
    started = time.monotonic()
    progress_updater.begin_cleanup_stage()
    try:
        renamed = tidy_markers_op(list(notes), config, progress_updater)
    except Exception as e:
        logger.error(f"Error tidying the sort field markers: {e}")
        print_error_traceback(e, logger)
        return None, []
    renamed_notes = [
        note for note in renamed.values() if note.id > 0 and note.id not in removed
    ]
    op_changes: Optional[OpChanges] = None
    if renamed_notes:
        try:
            col.update_notes(renamed_notes)
        except Exception as e:
            logger.error(f"Error updating notes after tidying their markers: {e}")
            print_error_traceback(e, logger)
        op_changes = col.merge_undo_entries(pos)
    log_phase("cleanup: tidy_markers_op", started, renamed=len(renamed_notes))
    return op_changes, [note.id for note in renamed_notes]


def selected_notes_op(
    done_text: str,
    bulk_op: Union[
        Callable[..., Coroutine[Any, Any, Optional[BulkOpResult]]], Sequence[OpPhase]
    ],
    nids: Sequence[NoteId],
    parent: Browser,
    progress_updater: AsyncTaskProgressUpdater,
    new_notes_op: Optional[NewNotesOp] = None,
    filter_new_notes_op: Optional[FilterNewNotesOp] = None,
    on_success: Optional[Callable] = None,
    unadded_notes_op: Optional[NewNotesOp] = None,
    tidy_markers_op: Optional[NewNotesOp] = None,
    chain: Optional[ChainStep] = None,
):
    """Run a bulk op, or a list of `OpPhase`s, over the selected notes as one operation.

    A list of phases runs them in order over the same notes and finishes with the same
    cleanup as a single op - see `run_op_phases` for what they share and what they do not.
    The new notes are added by `add_new_notes`, which says what the three note ops are for.
    `tidy_markers_op` gets every note the cleanup saved and added, last (see `tidy_markers`).

    With `chain`, the run is one step of a chain: its dialog title starts with the step's
    label, it shows no end message, and `chain.on_done` hears how it went, exactly once, on
    the main thread, after the progress is finished - on success, cancel, stop and exception
    alike. Without one, nothing here differs from a run from the menu.
    """
    phases = list(bulk_op) if isinstance(bulk_op, Sequence) else [OpPhase("", bulk_op)]
    edited_nids: list[NoteId] = []
    edited_other_nids: list[NoteId] = []
    notes_to_add_dict: dict[str, list[Note]] = {}
    notes_to_update_dict: dict[NoteId, Note] = {}
    notes_to_remove: set[NoteId] = set()
    new_notes = NewNotesCounts()
    config = mw.addonManager.getConfig(__name__) or {}
    nids_set = set(nids)
    # Whether the run was cancelled, for a chain step's outcome. Set on the op thread, read by
    # the success handler on the main thread once the op has returned.
    cancelled = False

    # Create a wrapper function that handles the async operation
    def run_bulk_op(col: Collection) -> OpChanges:
        nonlocal cancelled
        # Every operation enters here, which makes this the only place that can promise a run
        # starts uncancelled. bulk_notes_op and bulk_nested_notes_op used to do the clearing,
        # but an op is free to read the collection before it gets that far - the single-word
        # match ops search out the notes to work on first - and those reads go through
        # collection_access, which refuses while the previous, cancelled run's flag is still
        # set. That turned "cancel a run, then start another" into a RunCancelled traceback out
        # of the new operation before it had done anything.
        run = begin_run()
        # And the same for the cancel marker the diagnostics time everything against: a run
        # that starts after a cancelled one must not report its tasks as having returned
        # minutes after a cancel that belongs to the previous run.
        clear_cancel_time()

        async def async_wrapper():
            nonlocal edited_nids, edited_other_nids, new_notes, cancelled
            # Loaded once and handed to every phase, so a later phase sees the earlier
            # ones' writes and each note is written back to the collection only in cleanup
            notes = [mw.col.get_note(nid) for nid in nids]
            result = await run_op_phases(
                phases,
                col,
                notes=notes,
                edited_nids=edited_nids,
                progress_updater=progress_updater,
                notes_to_add_dict=notes_to_add_dict,
                notes_to_update_dict=notes_to_update_dict,
                label=done_text,
            )
            cleanup_started = time.monotonic()
            logger.debug("[phase] bulk op returned, starting cleanup")
            # From here on this thread is saving what the run managed to do, which is the whole
            # point of cancelling gracefully - so it keeps its access to the collection even
            # though the run is cancelled. Some ops have real work left here, such as resolving
            # the ids of the notes they added. Cleared in run_bulk_op's finally.
            begin_cleanup_phase()
            # Greys the buttons, and waits for that: nothing here heeds a cancel until
            # add_new_notes arms its own
            progress_updater.begin_cleanup()
            # Read once Cancel is grey, so every press made while it said it cancels the run
            # counts as the run's cancel, and before the note adding, which resets the flag to
            # give itself a cancel of its own. A sync op, or the gap between two phases, stops
            # on that flag alone without ever cancelling the run, so run_is_cancelled is not
            # enough.
            if mw.progress.want_cancel():
                cancelled = True
            pos, res_notes_to_add_dict, res_notes_to_update_dict, res_notes_to_remove = result

            sanitized_notes_to_remove: list[NoteId] = []
            if isinstance(res_notes_to_remove, str):
                logger.error("Invalid notes_to_remove payload type str: %s", res_notes_to_remove)
            else:
                try:
                    for nid in res_notes_to_remove:
                        if isinstance(nid, int):
                            sanitized_notes_to_remove.append(nid)
                        elif isinstance(nid, str) and nid.isdigit():
                            sanitized_notes_to_remove.append(NoteId(int(nid)))
                        else:
                            logger.error(
                                "Skipping invalid notes_to_remove nid type=%s value=%s",
                                type(nid),
                                nid,
                            )
                except TypeError:
                    logger.error(
                        "Invalid notes_to_remove payload non-iterable: %s",
                        res_notes_to_remove,
                    )

            # A cancelled run leaves its requests running in worker threads, and one of them
            # can still be writing into these dicts while we work through them here. Take a
            # snapshot so the cleanup sees a consistent set and can't trip over a dict that
            # changed size mid-iteration. The notes to add are the op's answer alone, never
            # the shared dict: bulk_nested_notes_op answers with the notes registered before
            # its flush, and one a thread registers after it, during the edited notes' write
            # below as likely as not, would be added with nothing linking to it.
            res_notes_to_add_dict = {
                word: list(word_notes) for word, word_notes in list(res_notes_to_add_dict.items())
            }
            res_notes_to_update_dict = dict(res_notes_to_update_dict)

            logger.debug(f"res_notes_to_update_dict keys: {res_notes_to_update_dict.keys()}")
            notes_to_update_dict.update(res_notes_to_update_dict)
            notes_to_remove.update(sanitized_notes_to_remove)
            logger.debug(f"notes_to_update_dict keys: {notes_to_update_dict.keys()}")
            for nid in res_notes_to_update_dict.keys():
                if nid not in nids_set:
                    edited_other_nids.append(nid)
            for nid in sanitized_notes_to_remove:
                if nid not in nids_set:
                    edited_other_nids.append(nid)
            edited_nids = [nid for nid in notes_to_update_dict if nid in nids_set]
            edited_nids.extend(
                nid
                for nid in dict.fromkeys(sanitized_notes_to_remove)
                if nid in nids_set and nid not in notes_to_update_dict
            )

            if notes_to_remove:
                for removed_nid in notes_to_remove:
                    if removed_nid in notes_to_update_dict:
                        del notes_to_update_dict[removed_nid]

            # Remove note.id=0 notes from updated_notes
            all_updated_notes_dict: dict[NoteId, Note] = {}
            for note in list(notes_to_update_dict.values()):
                if note.id == 0:
                    logger.error(f"Found note.id=0, fields: {note.fields}")
                elif note.id not in all_updated_notes_dict:
                    all_updated_notes_dict[note.id] = note
                else:
                    # If the note is already in notes_to_update_dict, this might be a problem in the
                    # logic of the bulk op
                    logger.warning(
                        f"Note {note.id} occurring multiple times in notes_to_update_dict during"
                        " bulk op final update"
                    )
            all_updated_notes = [n for n in all_updated_notes_dict.values() if n.id != 0]
            cleanup_started = log_phase(
                "cleanup: collect notes", cleanup_started, notes=len(all_updated_notes)
            )
            # This write has been the visible symptom of every cancellation hang so far, taking
            # minutes even with nothing to write. Record what the rest of the process is doing
            # on either side of it: an empty write cannot be slow by itself, so whatever is
            # holding it up is in these stacks.
            if run_cancelled():
                dump_thread_stacks("about to update_notes")
            try:
                mw.col.update_notes(all_updated_notes)
            except Exception as e:
                logger.error(f"Error updating notes: {e}")
                logger.error(f"Notes causing error: {[n.fields for n in all_updated_notes]}")
                print_error_traceback(e, logger)
            cleanup_started = log_phase("cleanup: update_notes", cleanup_started)
            if run_cancelled():
                dump_thread_stacks("finished update_notes")
            if notes_to_remove:
                try:
                    mw.col.remove_notes(sorted(notes_to_remove))
                except Exception as e:
                    logger.error(f"Error removing notes: {e}")
                    logger.error(f"Note IDs causing error: {sorted(notes_to_remove)}")
                    print_error_traceback(e, logger)
                cleanup_started = log_phase("cleanup: remove_notes", cleanup_started)
            op_changes = mw.col.merge_undo_entries(pos)
            # Every note saved from here on, for the marker tidying
            saved_notes: list[Note] = list(all_updated_notes)
            added_nids: set[NoteId] = set()
            notes_to_add = [
                note for word_notes in res_notes_to_add_dict.values() for note in word_notes
            ]
            cleanup_started = log_phase(
                "cleanup: merge_undo_entries", cleanup_started, to_add=len(notes_to_add)
            )

            if notes_to_add:
                added = add_new_notes(
                    mw.col,
                    notes_to_add,
                    config,
                    pos,
                    progress_updater,
                    new_notes_op,
                    filter_new_notes_op=filter_new_notes_op,
                    unadded_notes_op=unadded_notes_op,
                    notes_to_remove=notes_to_remove,
                )
                new_notes = added.counts
                if added.op_changes is not None:
                    op_changes = added.op_changes
                count_new_notes_edits(added, nids_set, edited_nids, edited_other_nids)
                saved_notes.extend(added.added_notes)
                saved_notes.extend(added.saved_notes)
                added_nids = {note.id for note in added.added_notes}
                cleanup_started = time.monotonic()
            if tidy_markers_op is not None and saved_notes:
                tidy_changes, tidied_nids = tidy_markers(
                    mw.col,
                    saved_notes,
                    config,
                    pos,
                    progress_updater,
                    tidy_markers_op,
                    notes_to_remove,
                )
                if tidy_changes is not None:
                    op_changes = tidy_changes
                # An added note renamed is counted as added
                count_edits(
                    [nid for nid in tidied_nids if nid not in added_nids],
                    nids_set,
                    edited_nids,
                    edited_other_nids,
                )
                cleanup_started = time.monotonic()
            log_phase("cleanup: finished", cleanup_started, threads=threading.active_count())
            return op_changes

        # Create and run the event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        # One bounded thread pool for the whole operation, shared by make_inner_bulk_op and
        # every asyncio.to_thread call beneath it. Previously each task built its own pool and
        # never shut it down, so threads accumulated for the life of the process.
        #
        # It is built before the gate exists, so it starts at the adaptive floor and the gate
        # sizes it properly the moment it knows its own ceiling - see set_run_executor and
        # executor_size. Nothing is submitted in between: `async_wrapper` loads the notes on
        # this thread, and both drivers build their gate before creating a single task.
        executor = ThreadPoolExecutor(
            max_workers=executor_size(0),
            thread_name_prefix="simple_anki_ai_prompts",
            # Every worker takes part in this run, so cancelling it stops their requests and
            # collection reads too - including in the threads the run is abandoning, which
            # keep the enrolment for as long as they are alive.
            initializer=partial(join_run, run),
        )
        loop.set_default_executor(executor)
        set_run_executor(executor)
        try:
            with bulk_op_logging():
                return loop.run_until_complete(async_wrapper())
        except RunCancelled as e:
            # A cancel that landed on a collection read this thread makes outside the cleanup
            # phase, so the op unwound before it could save anything. There is nothing left to
            # write, but being cancelled is a normal outcome rather than an error: end the
            # operation quietly instead of showing the user a traceback.
            logger.info("Bulk op abandoned after cancellation: %s", e)
            return OpChanges()
        finally:
            teardown_started = time.monotonic()
            logger.debug("[phase] teardown starting, %d threads alive", threading.active_count())
            # This thread goes back to Anki's pool and will run other operations, so its
            # exemption must not outlive this one
            end_cleanup_phase()
            set_run_executor(None)
            # shutdown(wait=False) tells the pool's threads to exit once their current call
            # returns, without blocking on them. Never join them here: a blocking HTTP request
            # cannot be interrupted from outside, so joining would make cancelling take as
            # long as the slowest request still in flight. Their results are discarded.
            executor.shutdown(wait=False)
            teardown_started = log_phase("teardown: executor.shutdown", teardown_started)
            # Tasks abandoned by a cancellation are still pending; drop them so closing the
            # loop doesn't complain about them
            leftover = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for task in leftover:
                task.cancel()
            teardown_started = log_phase(
                "teardown: cancel leftover tasks", teardown_started, leftover=len(leftover)
            )
            loop.close()
            teardown_started = log_phase("teardown: loop.close", teardown_started)
            close_all_sessions()
            log_phase(
                "teardown: close_all_sessions",
                teardown_started,
                threads=threading.active_count(),
            )
            # A run that is over while still paused - its op raised, or the user paused as the
            # last requests finished - must not leave its abandoned pool threads polling a pause
            # nobody will lift. Cancelled rather than resumed: a resume would let them send
            # requests for a run that has ended. Before end_run, while this thread still reads
            # the run it is ending.
            if pause_state() is not None:
                cancel_run()
            # After that cancel, which counts: a run that ended paused did not finish its
            # notes. The flag catches a cancel of the cleanup's note adding: once the cleanup has
            # greyed Cancel, nothing else sets it. Before end_run,
            # which forgets the run on this thread (run_cancelled would then say no).
            if run_is_cancelled(run) or mw.progress.want_cancel():
                cancelled = True
            # Last, so everything above still logs as part of the run it belongs to. This
            # thread is Anki's and goes back to a pool that runs other work, including our own
            # single-note ops, so its membership of this run must not outlive it: leaving it
            # enrolled in a cancelled run is what used to make every later op the editor hooks
            # run - a story or a translation on field unfocus - quietly do nothing for the
            # rest of the session. The run's own worker threads stay enrolled; they are the
            # ones that must keep seeing the cancellation.
            end_run()

    collection_op = CollectionOp(
        parent=parent,
        op=run_bulk_op,
    ).success(
        lambda out: on_bulk_success(
            out,
            done_text,
            edited_nids,
            edited_other_nids,
            nids,
            parent,
            on_success,
            new_notes=new_notes,
            chain=chain,
            cancelled=cancelled,
        )
    )
    if chain is not None:
        step = chain

        # Only for a chain: given a failure handler, aqt no longer shows the error itself,
        # so failed_step_outcome does. By then aqt has finished the step's progress, and in the
        # chain's dialog, still open, the error goes to its pane.
        def on_failure(error: Exception) -> None:
            step.on_done(failed_step_outcome(parent, error, step.title))

        collection_op.failure(on_failure)
        # Before the start, so the phase titles the op thread draws have it from the first
        progress_updater.set_title_prefix(f"{chain.label}: ")
    collection_op.run_in_background()
    # run_in_background opens the progress dialog before it returns (taskman.with_progress
    # calls progress.start on this, the main thread), so the buttons can go in now, before
    # the dialog is first shown. The op thread's redraws cannot overtake this: they are run
    # on this thread, after this function returns. A chain step's dialog is the chain's, which
    # the step before left with its buttons greyed; start_run_controls brings them back.
    start_run_controls()
    # A run from the menu built its updater, and set its title, before the start opened the
    # dialog, so that title was dropped. A chain step's dialog is the chain's and already had
    # it; drawing it again there costs nothing.
    progress_updater.show_title()
