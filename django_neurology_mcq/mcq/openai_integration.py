"""
OpenAI API Integration Module for the MCQ application.
Provides functions to interact with the OpenAI API for MCQ-related tasks.
Optimized for reliability, performance, and error handling.
"""

import os
import re
import json
import logging
import difflib
import time
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Optional, Tuple, Dict, Any, Union, List, Sequence

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[assignment]

try:
    from openai.types.responses.response_create_params import ResponseCreateParamsBase as _ResponseCreateParamsBase
except Exception:
    _ResponseCreateParamsBase = None  # type: ignore[assignment]

_RESPONSES_SUPPORTS_TOOL_RESOURCES = bool(
    getattr(_ResponseCreateParamsBase, "__annotations__", {}).get("tool_resources") if _ResponseCreateParamsBase else False
)
_RESPONSES_SUPPORTS_ATTACHMENTS = bool(
    getattr(_ResponseCreateParamsBase, "__annotations__", {}).get("attachments") if _ResponseCreateParamsBase else False
)

try:
    from openai import APIError, APIStatusError, APIConnectionError, RateLimitError
except ImportError:  # Defensive fallback if the client doesn't expose these helpers
    class _GenericAPIError(Exception):
        """Fallback error type when OpenAI helper classes are unavailable."""

    APIError = APIStatusError = APIConnectionError = RateLimitError = _GenericAPIError


# Configure logger
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
AGENT_RUNNER_PATH = PROJECT_ROOT / "agents" / "run_explanation_agent.js"
AGENT_RUN_TIMEOUT = int(os.environ.get("MCQ_AGENT_TIMEOUT_SECONDS", "120"))
USE_AGENT_FOR_EXPLANATIONS = os.environ.get("MCQ_USE_AGENT_EXPLANATION", "1").lower() not in {"0", "false", "no"}

# Attempt to load environment variables early so API keys are available even
# when this module is imported outside the Django settings lifecycle.
if load_dotenv:
    _dot_env_path = PROJECT_ROOT / ".env"
    if _dot_env_path.exists():
        load_dotenv(_dot_env_path)
    else:
        # Fall back to default behaviour (cwd/.env) to accommodate other setups.
        load_dotenv()
# Environment detection
IS_RAILWAY = os.environ.get('RAILWAY_ENVIRONMENT') == 'production'
IS_HEROKU = os.environ.get('DYNO') is not None
ENVIRONMENT = 'Railway' if IS_RAILWAY else 'Heroku' if IS_HEROKU else 'Local'

logger.info(f"Running in {ENVIRONMENT} environment")

# Configuration constants
MAX_INITIALIZATION_ATTEMPTS = 3
# Increase default timeout to accommodate long generations/transcriptions
DEFAULT_TIMEOUT = 90
OPENAI_API_KEY_VARS = ['OPENAI_API_KEY', 'OPENAI_KEY']
# Allow overriding the model via environment; default to a current mini model
# Use GPT-5-mini by default across features unless overridden
# GPT-5-mini was released August 2025 - cost-efficient with 400K token context
_MODEL_FROM_ENV = os.environ.get('OPENAI_MODEL', '').strip()
_FALLBACK_FROM_ENV = os.environ.get('OPENAI_FALLBACK_MODEL', '').strip()
DEFAULT_MODEL = _MODEL_FROM_ENV or "gpt-5-mini"
# Keep fallback aligned with the default model to avoid unintended downgrades
FALLBACK_MODEL = _FALLBACK_FROM_ENV or DEFAULT_MODEL
QUESTION_MIN_WORDS = int(os.environ.get("QUESTION_AI_MIN_WORDS", "45"))
QUESTION_MIN_CHARS = int(os.environ.get("QUESTION_AI_MIN_CHARS", "220"))
QUESTION_SIMILARITY_THRESHOLD = float(os.environ.get("QUESTION_AI_SIMILARITY", "0.92"))
AI_PAL_MODEL = DEFAULT_MODEL

_FALLBACK_VECTOR_STORE_ID = "vs_6907b938bc248191944e5c224c545d47"
_vector_from_env = os.environ.get("OPENAI_VECTOR_STORE_ID")
if _vector_from_env is not None:
    _vector_from_env = _vector_from_env.strip()
VECTOR_STORE_ID = _vector_from_env or _FALLBACK_VECTOR_STORE_ID
if VECTOR_STORE_ID and VECTOR_STORE_ID.lower() in {"none", "disable", "disabled"}:
    VECTOR_STORE_ID = None
KNOWLEDGE_VECTOR_AVAILABLE = bool(VECTOR_STORE_ID)

# Client state tracking
_initialization_attempts = 0
api_key = None
client = None

# Models available in the account (cached to avoid redundant API calls)
_available_models = []


def initialize_openai_client() -> Tuple[Optional[str], Optional[Any]]:
    """
    Initialize the OpenAI client with robust retry and validation logic.
    
    Returns:
        Tuple of (api_key, client) where client may be None if initialization fails
    """
    global _initialization_attempts
    
    # Prevent excessive retry attempts
    if _initialization_attempts >= MAX_INITIALIZATION_ATTEMPTS:
        logger.warning(f"Exceeded maximum OpenAI initialization attempts ({MAX_INITIALIZATION_ATTEMPTS})")
        return None, None
    
    _initialization_attempts += 1
    
    # Try different possible environment variable names
    api_key = None
    for env_var in OPENAI_API_KEY_VARS:
        api_key = os.environ.get(env_var)
        if api_key:
            logger.info(f"Found API key in {env_var}")
            break
    
    if not api_key:
        logger.warning("No OpenAI API key found in environment variables")
        return None, None
    
    # Confirm key presence without logging identifiable detail
    logger.info("OpenAI API key detected in environment variables.")
    
    if not api_key.startswith('sk-'):
        logger.warning("API key has unusual format (doesn't start with 'sk-')")
    
    # Initialize OpenAI client with error handling
    try:
        from openai import OpenAI
        
        # Create the client with timeout
        client = OpenAI(api_key=api_key, timeout=DEFAULT_TIMEOUT)
        
        # Verify the client with a lightweight API call
        if verify_openai_client(client):
            return api_key, client
        else:
            return api_key, None
            
    except ImportError:
        logger.error("Failed to import OpenAI package. Check that it's installed.")
        return api_key, None
    except Exception as e:
        logger.error(f"Failed to initialize OpenAI client: {str(e)}")
        return api_key, None


def verify_openai_client(client: Any) -> bool:
    """
    Verify that the OpenAI client is working by making a test API call.
    Populate the available models cache if successful.
    
    Args:
        client: The OpenAI client to verify
        
    Returns:
        bool: True if client is working, False otherwise
    """
    global _available_models
    
    try:
        # Make a lightweight API call to verify connectivity
        models = client.models.list()
        model_list = list(models.data)
        
        if model_list:
            # Cache available models for later use
            _available_models = [model.id for model in model_list]
            sample_models = _available_models[:3]  # Show just a few models
            
            logger.info(f"✅ OpenAI client verified successfully. Found {len(_available_models)} models.")
            logger.info(f"Sample models: {', '.join(sample_models)}...")
            return True
        else:
            logger.warning("API call succeeded but returned no models")
            return True  # Still return True as the API itself worked
                
    except Exception as api_test_error:
        error_msg = str(api_test_error)
        logger.error(f"API connectivity test failed: {error_msg}")
        
        # Handle common errors gracefully
        if "Rate limit" in error_msg:
            logger.warning("Rate limit error detected. This might be temporary.")
            return True  # Return True since client is valid, just rate limited
        
        if any(term in error_msg.lower() for term in ["timeout", "connection", "network"]):
            logger.warning("Network-related error. Client may still work for future requests.")
            return True  # Return True for temporary network issues
                
        return False


def _sanitize_for_policy(text: str, *, limit: int = 800) -> str:
    """Sanitize free-form text to reduce policy violations in prompts."""
    if not text:
        return ""
    cleaned = re.sub(r"[^A-Za-z0-9.,;:()\-\s]", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:limit]


def _run_agent_explanation(
    prompt: str,
    *,
    workflow_id: str = "",
    context: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """
    Execute the external agent runner to generate an explanation.

    Args:
        prompt: Fully formatted prompt text passed to the agent SDK runner
        workflow_id: Optional trace identifier forwarded to the runner
        context: Optional metadata for downstream logging (unused by runner)

    Returns:
        The explanation string if the agent runner succeeds, otherwise None.
    """
    if not USE_AGENT_FOR_EXPLANATIONS:
        return None

    if not AGENT_RUNNER_PATH.exists():
        logger.debug(
            "Agent runner script not found at %s; skipping agent integration",
            AGENT_RUNNER_PATH,
        )
        return None

    payload: Dict[str, Any] = {"prompt": prompt}
    if workflow_id:
        payload["workflow_id"] = workflow_id
    if context:
        payload["context"] = context

    try:
        completed = subprocess.run(
            ["node", str(AGENT_RUNNER_PATH)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=True,
            timeout=AGENT_RUN_TIMEOUT,
        )
    except FileNotFoundError:
        logger.warning("Node.js executable not available; skipping agent runner.")
        return None
    except subprocess.TimeoutExpired:
        logger.warning(
            "Agent runner timed out after %s seconds; falling back to Python client.",
            AGENT_RUN_TIMEOUT,
        )
        return None
    except subprocess.CalledProcessError as exc:
        stderr_snippet = (exc.stderr or "").strip()
        if stderr_snippet:
            logger.warning(
                "Agent runner failed with exit code %s: %s",
                exc.returncode,
                stderr_snippet[:300],
            )
        else:
            logger.warning(
                "Agent runner failed with exit code %s and no stderr output.",
                exc.returncode,
            )
        return None
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("Unexpected error invoking agent runner: %s", exc)
        return None

    stdout = (completed.stdout or "").strip()
    if not stdout:
        logger.warning("Agent runner returned empty stdout; skipping result.")
        return None

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        logger.warning(
            "Agent runner emitted non-JSON output: %s",
            stdout[:300],
        )
        return None

    explanation = str(data.get("explanation", "")).strip()
    if not explanation:
        logger.warning("Agent runner JSON missing 'explanation' field.")
        return None

    logger.info("Explanation generated via agent SDK runner.")
    return explanation


def extract_message_text(message: Any) -> str:
    """
    Normalise the content of an OpenAI message to a plain string.

    Handles both legacy chat responses where ``content`` is a string and the newer
    responses where ``content`` is represented as a list of typed blocks.
    """
    if message is None:
        return ""

    content = getattr(message, "content", None)
    text = ""

    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                part_text = item.get("text")
                if part_text:
                    parts.append(part_text)
            elif hasattr(item, "text"):
                part_text = getattr(item, "text", "")
                if part_text:
                    parts.append(part_text)
        text = "".join(parts)
    elif content is not None:
        text = str(content)

    text = (text or "").strip()

    if not text:
        refusal = getattr(message, "refusal", None)
        if refusal:
            reason = getattr(refusal, "reason", "")
            if reason:
                text = reason.strip()
            else:
                text = str(refusal).strip()

    return text


def get_first_choice_text(response: Any) -> str:
    """
    Convenience helper to extract text from the first choice of a chat completion.
    """
    if not response or not getattr(response, "choices", None):
        return ""
    message = response.choices[0].message
    return extract_message_text(message)


# Initialize the API key and client
api_key, client = initialize_openai_client()

# Record client initialization status for monitoring
OPENAI_STATUS = {
    'initialized': client is not None,
    'initialization_time': time.time(),
    'api_key_available': api_key is not None,
    'environment': ENVIRONMENT,
    'default_model': DEFAULT_MODEL,
    'fallback_model': FALLBACK_MODEL,
    'models_available': len(_available_models)
}

# Log initialization result
if client:
    logger.info("✅ OpenAI integration is active and ready for use")
else:
    logger.warning("⚠️ OpenAI integration unavailable. Using mock responses instead.")
    logger.info("To enable AI features, set the OPENAI_API_KEY environment variable.")
    
    # Environment-specific guidance
    if IS_HEROKU:
        logger.info("On Heroku, use: heroku config:set OPENAI_API_KEY=your-api-key")
    elif IS_RAILWAY:
        logger.info("On Railway, set the OPENAI_API_KEY in the project variables")
    else:
        logger.info("For local development, add OPENAI_API_KEY to your .env file")

# Mock response generation
# ---------------------
# These functions generate high-quality mock responses when OpenAI API is unavailable

def get_mock_explanation() -> str:
    """
    Generate a mock MCQ explanation with proper formatting.
    Used as a fallback when OpenAI API is unavailable.
    
    Returns:
        str: Formatted explanation text
    """
    return """
    # EXPLANATION OF MCQ: Neurological Assessment and Diagnosis
    
    ## Question Recap
    The question presents a case requiring careful assessment of neurological signs and symptoms.
    
    ## Why the Correct Answer is Right
    The correct answer applies evidence-based diagnostic criteria from current guidelines. Key factors supporting this answer include:
    - Characteristic pattern of symptoms with high specificity 
    - Temporal progression consistent with the diagnosis
    - Appropriate correlation between clinical findings and neuroanatomy
    
    ## Why Each Wrong Answer is Wrong
    The incorrect options represent common misconceptions or partial understandings:
    - They may focus on atypical presentations
    - Some apply outdated diagnostic criteria
    - Others misinterpret the significance of key findings
    
    ## Comparison Table of Options
    | Consideration | Option A | Option B | Option C | Option D |
    |---------------|----------|----------|----------|----------|
    | Evidence base | Limited  | Strong   | Moderate | Weak     |
    | Guidelines    | Outdated | Current  | Mixed    | Emerging |
    | Specificity   | 60%      | 95%      | 75%      | 45%      |
    | Sensitivity   | 85%      | 92%      | 70%      | 65%      |
    
    ## Clinical Pearls to Memorize
    - Always correlate clinical findings with neuroanatomical pathways
    - Consider the timing and progression of symptoms
    - Look for localizing signs on examination
    - Remember that common conditions present commonly
    - Always rely on evidence-based diagnostic approaches
    
    ## Summary of Latest Guidelines
    The American Academy of Neurology (2024) guidelines recommend:
    - Systematic assessment of neurological signs and symptoms
    - Appropriate use of diagnostic tests in logical sequence
    - Consideration of both common and rare conditions
    - Application of evidence-based treatment approaches
    
    ## Historical Note
    Understanding of this condition has evolved significantly, with major advances in diagnostic accuracy and treatment options over the past decade.
    
    References:
    1. Adams and Victor's Principles of Neurology, 12th Edition
    2. American Academy of Neurology Guidelines (2024)
    3. European Academy of Neurology Consensus Statement (2023)
    """


def get_mock_improved_question(original_question: str) -> str:
    """
    Return an improved version of the original question text.
    Enhances clarity and formatting without changing content.
    
    Args:
        original_question: The original question text to improve
        
    Returns:
        str: The improved question text
    """
    if not original_question:
        return "No question provided."
    
    # Clean up whitespace issues
    improved = original_question.strip()
    improved = re.sub(r'\s{2,}', ' ', improved)
    
    # Ensure proper sentence endings
    if not any(improved.endswith(char) for char in ['.', '?', '!']):
        improved += '?'
    
    # Add proper paragraph breaks
    if len(improved) > 200 and '. ' in improved:
        sentences = improved.split('. ')
        if len(sentences) >= 3:
            # Group sentences into logical paragraphs
            middle = len(sentences) // 2
            first_part = '. '.join(sentences[:middle]) + '.'
            second_part = '. '.join(sentences[middle:])
            if not second_part.endswith('.'):
                second_part += '.'
            improved = f"{first_part}\n\n{second_part}"
    
    return improved


def get_mock_options() -> Dict[str, str]:
    """
    Generate mock MCQ options with plausible neurological content.
    
    Returns:
        dict: Dictionary of option letters to option text
    """
    return {
        "A": "Multiple sclerosis - suggested by the relapsing-remitting pattern and multifocal neurological deficits",
        "B": "Acute ischemic stroke - supported by the sudden onset and focal neurological deficit in a vascular distribution",
        "C": "Guillain-Barré syndrome - characterized by progressive ascending weakness and areflexia",
        "D": "Migraine with aura - indicated by the visual disturbances preceding the headache",
        "E": "Temporal lobe epilepsy - explains the episodes of altered awareness and automatisms"
    }


def _normalize_question_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _question_word_count(text: str) -> int:
    if not text:
        return 0
    return len(re.findall(r"\b\w+\b", text))


def _extract_option_labels(text: str) -> Dict[str, str]:
    """
    Extract answer options labelled with A/B/C/D (supports formats like "A.", "A)", "A -").
    Returns a dict mapping option letter to its content.
    """
    options: Dict[str, str] = {}
    if not text:
        return options
    pattern = re.compile(r"^\s*([A-D])[\)\.\-\:]\s*(.+)", re.IGNORECASE | re.MULTILINE)
    for match in pattern.finditer(text):
        letter = match.group(1).upper()
        body = match.group(2).strip()
        if body:
            options[letter] = body
    return options


_FORBIDDEN_EXCLUDE_TERMS = {
    "question stem",
    "stem",
    "question",
    "prompt",
    "this question",
    "it",
    "them",
    "that",
    "these",
    "any mention",
    "any references",
}


def _extract_forbidden_terms(custom_instructions: str) -> List[str]:
    if not custom_instructions:
        return []

    text = custom_instructions.lower()
    patterns = [
        r"do not (?:mention|include|use|reference)\s+([^.;\n]+)",
        r"avoid\s+([^.;\n]+)",
        r"without (?:mentioning|including|referencing)\s+([^.;\n]+)",
        r"never (?:mention|include)\s+([^.;\n]+)",
    ]

    terms: set[str] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            clause = match.group(1)
            if not clause:
                continue
            clause = re.sub(r"[^a-z0-9\s/-]", " ", clause.lower())
            fragments = re.split(r",|/|;|\band\b|\bor\b", clause)
            for fragment in fragments:
                cleaned = fragment.strip()
                if not cleaned or cleaned in _FORBIDDEN_EXCLUDE_TERMS:
                    continue
                if len(cleaned) < 3:
                    continue
                terms.add(re.sub(r"\s+", " ", cleaned))

    return sorted(terms)


def _detect_forbidden_hits(text: str, forbidden_terms: Sequence[str]) -> List[str]:
    if not text or not forbidden_terms:
        return []
    normalized = _normalize_question_text(text)
    hits = []
    for term in forbidden_terms:
        term_clean = re.sub(r"\s+", " ", term.strip().lower())
        if term_clean and term_clean in normalized:
            hits.append(term_clean)
    return hits


def _validate_question_revision(
    *,
    original: str,
    revised: str,
    wants_options: bool,
    forbidden_terms: Sequence[str] | None = None,
) -> List[str]:
    errors: List[str] = []
    normalized_original = _normalize_question_text(original)
    normalized_revised = _normalize_question_text(revised)

    if not normalized_revised:
        errors.append("AI returned an empty question.")
        return errors

    word_count = _question_word_count(revised)
    char_count = len(revised.strip())

    if char_count < QUESTION_MIN_CHARS:
        errors.append(
            f"Revised question is too short ({char_count} chars); needs ≥ {QUESTION_MIN_CHARS}."
        )
    if word_count < QUESTION_MIN_WORDS:
        errors.append(
            f"Revised question has too few words ({word_count}); needs ≥ {QUESTION_MIN_WORDS}."
        )

    if normalized_original and normalized_revised == normalized_original:
        errors.append("Revised question is identical to the original.")
    else:
        similarity = difflib.SequenceMatcher(None, normalized_original, normalized_revised).ratio()
        if similarity >= QUESTION_SIMILARITY_THRESHOLD:
            errors.append(
                f"Revised question is too similar to the original (similarity {similarity:.2f})."
            )

    if wants_options:
        options = _extract_option_labels(revised)
        if len(options) != 4:
            errors.append("Multiple-choice request requires exactly four answer options (A-D).")
        else:
            missing = {letter for letter in ("A", "B", "C", "D") if letter not in options}
            if missing:
                errors.append(f"Missing answer options: {', '.join(sorted(missing))}.")
            for letter, body in options.items():
                if len(body) < 5:
                    errors.append(f"Option {letter} is too short to be plausible.")
    else:
        if _extract_option_labels(revised):
            errors.append("Question stem mode should not include labelled answer options.")

    forbidden_hits = _detect_forbidden_hits(revised, forbidden_terms or [])
    if forbidden_hits:
        errors.append(
            "Question includes prohibited terms: "
            + ", ".join(sorted(set(forbidden_hits)))
        )

    return errors


def _classify_openai_error(error: Exception) -> Tuple[bool, str, Optional[int]]:
    """Determine whether an OpenAI exception should trigger a retry."""

    status_code = getattr(error, "status_code", None) or getattr(error, "http_status", None)
    if isinstance(status_code, str) and status_code.isdigit():
        status_code = int(status_code)

    if isinstance(error, (APIConnectionError, TimeoutError)):
        return True, "OpenAI connection error", status_code

    if isinstance(error, RateLimitError):
        return True, "OpenAI rate limit exceeded", status_code

    if status_code and int(status_code) >= 500:
        return True, f"OpenAI service unavailable (HTTP {status_code})", status_code

    if isinstance(error, APIError):
        message = getattr(error, "message", None) or str(error)
        return False, message, status_code

    return False, str(error), status_code


def _default_vector_tools() -> Optional[List[Dict[str, str]]]:
    if not KNOWLEDGE_VECTOR_AVAILABLE:
        return None
    return [{"type": "file_search"}]


def _default_vector_attachments() -> Optional[List[Dict[str, str]]]:
    if not KNOWLEDGE_VECTOR_AVAILABLE:
        return None
    return [{"vector_store_id": VECTOR_STORE_ID}]


def _default_tool_resources() -> Optional[Dict[str, Any]]:
    """
    Build tool_resources payload for Responses API file_search tool.
    Preferred over legacy 'attachments' for vector stores.
    """
    if not KNOWLEDGE_VECTOR_AVAILABLE:
        return None
    return {"file_search": {"vector_store_ids": [VECTOR_STORE_ID]}}


def _extract_response_text(response: Any) -> str:
    """
    Extract plain text from a Responses API call, handling both text blocks and JSON schema outputs.
    """
    if response is None:
        return ""

    text = getattr(response, "output_text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()

    output_items = getattr(response, "output", None)
    if output_items:
        parts: List[str] = []
        for item in output_items:
            content_list = getattr(item, "content", None)
            if not content_list:
                continue
            for block in content_list:
                block_type = None
                block_obj = block
                if isinstance(block, dict):
                    block_type = block.get("type")
                else:
                    block_type = getattr(block, "type", None)
                if block_type in {"output_text", "text"}:
                    if isinstance(block, dict):
                        block_text = block.get("text")
                    else:
                        block_text = getattr(block, "text", None)
                    if block_text:
                        parts.append(str(block_text))
                elif block_type == "json_schema":
                    # Some SDK builds expose parsed JSON via .json attribute
                    if isinstance(block, dict):
                        json_payload = block.get("json")
                    else:
                        json_payload = getattr(block, "json", None)
                    if json_payload:
                        try:
                            parts.append(json.dumps(json_payload))
                        except Exception:
                            pass
        if parts:
            return "".join(parts).strip()

    text_body = getattr(response, "text", None)
    if isinstance(text_body, str) and text_body.strip():
        return text_body.strip()

    data = getattr(response, "data", None)
    if isinstance(data, str):
        return data.strip()

    return ""


def _extract_json_from_text(text: str) -> Optional[Dict[str, Any]]:
    """
    Manually extract JSON from text that might contain explanations or other content.
    This is a fallback for when GPT models ignore JSON formatting instructions.
    """
    if not text or not isinstance(text, str):
        return None

    text = text.strip()

    # Try to find JSON object boundaries
    start_idx = text.find('{')
    end_idx = text.rfind('}')

    if start_idx == -1 or end_idx == -1 or start_idx >= end_idx:
        return None

    # Extract potential JSON substring
    json_candidate = text[start_idx:end_idx + 1]

    # Clean up common issues
    json_candidate = json_candidate.strip()

    try:
        parsed = json.loads(json_candidate)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        # Try to fix common JSON issues
        # Remove trailing commas
        json_candidate = json_candidate.replace(',}', '}').replace(',]', ']')

        # Try again
        try:
            parsed = json.loads(json_candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    return None


def _extract_response_json(response: Any) -> Tuple[Dict[str, Any], str]:
    """
    Attempt to pull a JSON object from a Responses API or Chat Completions API output.
    Returns (payload, raw_text).
    """
    if response is None:
        return {}, ""

    # Try Responses API format first
    output_items = getattr(response, "output", None)
    if output_items:
        for item in output_items:
            content_list = getattr(item, "content", None)
            if not content_list:
                continue
            for block in content_list:
                if isinstance(block, dict):
                    if block.get("type") == "json_schema":
                        json_payload = block.get("json")
                        if isinstance(json_payload, dict):
                            return json_payload, json.dumps(json_payload)
                else:
                    block_type = getattr(block, "type", None)
                    if block_type == "json_schema":
                        json_payload = getattr(block, "json", None)
                        if isinstance(json_payload, dict):
                            return json_payload, json.dumps(json_payload)

    # Try Chat Completions API format
    choices = getattr(response, "choices", None)
    if choices and len(choices) > 0:
        message = getattr(choices[0], "message", None)
        if message:
            content = getattr(message, "content", "")
            logger.info(f"Chat completion message content type: {type(content)}, length: {len(content) if content else 0}")
            if content and isinstance(content, str):
                # Log first 200 chars of content for debugging
                logger.info(f"Content preview: {content[:200]}")
                # Check if content looks like HTML (common error response)
                if content.strip().startswith('<'):
                    logger.warning("Response content appears to be HTML, not JSON")
                    return {}, ""
                try:
                    payload = json.loads(content)
                    if isinstance(payload, dict):
                        logger.info(f"Successfully parsed JSON with {len(payload)} keys")
                        return payload, content
                except json.JSONDecodeError as e:
                    logger.warning(f"Failed to parse response content as JSON: {e}")
                    logger.warning(f"Content causing error: {content[:500]}")

                    # Fallback: Try to extract JSON manually from the content
                    # This handles cases where GPT-5 mini ignores response_format instructions
                    manual_payload = _extract_json_from_text(content)
                    if manual_payload:
                        logger.info(f"Successfully extracted JSON manually with {len(manual_payload)} keys")
                        return manual_payload, content
                    pass
            else:
                logger.warning(f"Message content is empty or not a string: {content}")

    # Fallback to extracting text and parsing
    raw_text = _extract_response_text(response)
    if not raw_text:
        return {}, ""

    # Check if raw_text looks like HTML before attempting to parse
    if raw_text.strip().startswith('<'):
        logger.warning("Raw response text appears to be HTML, not JSON")
        return {}, ""

    try:
        payload = json.loads(raw_text)
        if isinstance(payload, dict):
            return payload, raw_text
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse raw response text as JSON: {e}")

        # Fallback: Try to extract JSON manually from the raw text
        manual_payload = _extract_json_from_text(raw_text)
        if manual_payload:
            logger.info(f"Successfully extracted JSON manually from raw text with {len(manual_payload)} keys")
            return manual_payload, raw_text

        pass
    return {}, raw_text


def _select_chat_model(preferred: str) -> str:
    """
    Choose the chat-completions model when falling back from Responses API.
    For consistency, we keep all flows on GPT-5 mini unless explicitly overridden.
    """
    candidate = (preferred or "").strip()
    if candidate and candidate.lower().startswith("gpt-5"):
        return candidate

    env_override = os.environ.get("OPENAI_MODEL", "").strip()
    if env_override and env_override.lower().startswith("gpt-5"):
        return env_override

    return DEFAULT_MODEL


def _responses_create(
    model: str,
    input_items: Sequence[Dict[str, Any]],
    *,
    use_vector: bool = False,
    attachments: Optional[List[Dict[str, Any]]] = None,
    tool_resources: Optional[Dict[str, Any]] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    **kwargs: Any,
) -> Any:
    """
    Helper around client.responses.create with consistent parameter handling.
    Falls back to chat.completions if responses API is unavailable.
    """
    if not client:
        raise RuntimeError("OpenAI client is unavailable.")
    
    # Map compatibility parameters before making any API calls
    response_format = kwargs.pop("response_format", None)
    text_config = kwargs.pop("text", None)
    tool_resources = kwargs.pop("tool_resources", tool_resources)

    # Normalise token parameter names
    if "max_tokens" in kwargs and "max_output_tokens" not in kwargs:
        kwargs["max_output_tokens"] = kwargs.pop("max_tokens")

    responses_kwargs = dict(kwargs)
    chat_kwargs = dict(kwargs)

    # Prepare responses payload if the Responses API is available
    responses_payload: Optional[Dict[str, Any]] = None
    if hasattr(client, "responses"):
        responses_payload = {
            "model": model,
            "input": [],
        }

        # Convert chat-style items into Responses API format
        for item in input_items:
            if not isinstance(item, dict):
                continue
            role = item.get("role", "user")
            content = item.get("content", "")

            # Support legacy list-based content from chat.completions
            if isinstance(content, list):
                responses_payload["input"].append(
                    {
                        "type": "message",
                        "role": role,
                        "content": [
                            {"type": "input_text", "text": str(block.get("text", ""))}
                            for block in content
                            if isinstance(block, dict) and block.get("type") == "text"
                        ],
                    }
                )
            else:
                responses_payload["input"].append(
                    {
                        "type": "message",
                        "role": role,
                        "content": [
                            {"type": "input_text", "text": str(content)},
                        ],
                    }
                )

        # Configure text + reasoning options for GPT-5 models
        is_gpt5 = str(model).startswith("gpt-5")
        default_verbosity = os.environ.get("OPENAI_TEXT_VERBOSITY", "low")
        default_reasoning = os.environ.get("OPENAI_REASONING_EFFORT", "medium")

        if is_gpt5:
            responses_payload.setdefault("reasoning", {"effort": default_reasoning})

        if text_config:
            responses_payload["text"] = text_config
        elif response_format:
            if is_gpt5 and isinstance(response_format, dict):
                format_payload: Dict[str, Any] = {}
                fmt_type = response_format.get("type")
                if fmt_type == "json_schema":
                    json_def = response_format.get("json_schema", {}) or {}
                    schema_def = json_def.get("schema")
                    if schema_def is None:
                        raise ValueError("json_schema response_format requires a 'schema' definition")
                    format_payload["type"] = "json_schema"
                    format_payload["schema"] = schema_def
                    if "name" in json_def:
                        format_payload["name"] = json_def["name"]
                else:
                    format_payload["type"] = fmt_type or "text"
                    for key, value in response_format.items():
                        if key not in {"type"}:
                            format_payload[key] = value
                responses_payload["text"] = {
                    "verbosity": default_verbosity,
                    "format": format_payload,
                }
            else:
                responses_payload["text"] = {"format": response_format}
        elif is_gpt5:
            responses_payload["text"] = {"verbosity": default_verbosity}

        if "temperature" in responses_kwargs and str(model).startswith("gpt-5"):
            # GPT-5 models ignore sampling parameters; drop them to avoid API warnings
            responses_kwargs.pop("temperature", None)
            responses_kwargs.pop("top_p", None)
            responses_kwargs.pop("presence_penalty", None)
            responses_kwargs.pop("frequency_penalty", None)

        # Attach vector store if requested
        vector_attachments = attachments
        if use_vector and KNOWLEDGE_VECTOR_AVAILABLE and not attachments:
            if _RESPONSES_SUPPORTS_ATTACHMENTS or _RESPONSES_SUPPORTS_TOOL_RESOURCES:
                vector_attachments = _default_vector_attachments() if _RESPONSES_SUPPORTS_ATTACHMENTS else None
                if _RESPONSES_SUPPORTS_TOOL_RESOURCES:
                    tool_resources = tool_resources or _default_tool_resources()
                if vector_attachments or tool_resources:
                    tools = tools or _default_vector_tools()
            else:
                vector_attachments = None

        if tool_resources and _RESPONSES_SUPPORTS_TOOL_RESOURCES:
            responses_payload["tool_resources"] = tool_resources
        if vector_attachments and _RESPONSES_SUPPORTS_ATTACHMENTS:
            responses_payload["attachments"] = vector_attachments
        if tools:
            responses_payload["tools"] = tools

    # Try using Responses API if available and the payload was formed correctly
    if responses_payload is not None:
        try:
            responses_payload["input"] = list(responses_payload["input"])
            responses_payload.update(responses_kwargs)
            logger.info(f"Calling Responses API with model: {model}")
            response = client.responses.create(**responses_payload)
            if response is None:
                raise RuntimeError("Responses API returned None")
            return response
        except Exception as e:
            logger.warning(f"Responses API failed: {str(e)}, falling back to chat completions")
            # Fall through to chat completions
            if str(model).startswith("gpt-5"):
                raise
    else:
        logger.info("Responses API not available, using chat completions")

    # Fallback to chat.completions.create
    # Convert input_items to messages format
    messages = []
    for item in input_items:
        if isinstance(item, dict):
            role = item.get("role", "user")
            content = item.get("content", "")
            # Handle both string content and list of content blocks
            if isinstance(content, list):
                # Extract text from content blocks
                text_parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                content = "\n".join(text_parts)
            messages.append({"role": role, "content": content})

    chat_model = _select_chat_model(model)

    if str(chat_model).startswith("gpt-5"):
        chat_kwargs.pop("temperature", None)
        chat_kwargs.pop("top_p", None)
        chat_kwargs.pop("presence_penalty", None)
        chat_kwargs.pop("frequency_penalty", None)

    # Convert max_output_tokens to appropriate parameter for chat completions
    # GPT-5 models use max_completion_tokens, while older models use max_tokens
    if "max_output_tokens" in chat_kwargs:
        max_tokens_value = chat_kwargs.pop("max_output_tokens")
        if str(chat_model).startswith("gpt-5"):
            chat_kwargs["max_completion_tokens"] = max_tokens_value
        else:
            chat_kwargs["max_tokens"] = max_tokens_value

    # Handle response_format - GPT-5 mini is incompatible with json_object format
    logger.info(f"response_format before conversion: {response_format}")
    if response_format:
        # GPT-5 mini doesn't support response_format properly - use prompt-only approach
        if str(chat_model).startswith("gpt-5"):
            logger.info("GPT-5 detected - removing response_format and relying on prompt instructions")
            # Add explicit JSON instruction to the first user message
            if messages and not any("JSON" in str(m.get("content", "")) for m in messages[:2]):
                logger.info("Adding explicit JSON output instruction to prompt for GPT-5")
                messages[0]["content"] = messages[0]["content"] + "\n\nIMPORTANT: You must respond with valid JSON only. Do not include any other text or explanations."
        else:
            # For other models, use normal json_schema to json_object conversion
            if isinstance(response_format, dict):
                response_type = response_format.get("type")
                logger.info(f"response_format type: {response_type}")
                if response_type == "json_schema":
                    logger.info("Converting json_schema to json_object for chat completions API")
                    chat_kwargs["response_format"] = {"type": "json_object"}
                    # Add explicit JSON instruction to the first user message if not already present
                    if messages and not any("JSON" in str(m.get("content", "")) for m in messages[:2]):
                        logger.info("Adding explicit JSON output instruction to prompt")
                        messages[0]["content"] = messages[0]["content"] + "\n\nYou must respond with valid JSON only."
                else:
                    chat_kwargs["response_format"] = response_format
            else:
                chat_kwargs["response_format"] = response_format

    logger.info(f"Calling chat completions API with model: {chat_model}")
    try:
        response = client.chat.completions.create(
            model=chat_model,
            messages=messages,
            **chat_kwargs
        )
        # Verify the response is valid before returning
        if response is None:
            raise RuntimeError("Chat completions API returned None")
        return response
    except Exception as e:
        logger.error(f"Chat completions API failed: {str(e)}")
        raise


def get_mock_verification() -> str:
    """
    Generate a mock answer verification response with realistic analysis.
    
    Returns:
        str: Formatted verification text
    """
    return """# Answer Verification Analysis

## Evidence Summary 📊
Based on current guidelines from the American Academy of Neurology (AAN, 2024) and European Academy of Neurology (EAN, 2023), the marked answer aligns with evidence-based standards for this clinical scenario.

## Correct Answer: Option B ✅
The correct answer is indeed Option B, supported by:
- Level A evidence from three large randomized controlled trials
- Meta-analysis of 12 studies with over 5,000 patients
- Current AAN guidelines (2024) supporting this as standard of care
- Clear pathophysiological mechanism established in the literature

## Incorrect Options Analysis ❌
- Option A: While plausible, lacks evidence from rigorous studies and contradicts current guidelines
- Option C: Outdated approach based on older literature; no longer recommended
- Option D: Only applicable in specific circumstances not present in this case
- Option E: Represents a common misconception about the condition

## Question Quality Assessment 🔍
This is a high-quality question with:
- Clear clinical scenario
- Well-defined options
- Up-to-date content aligned with current practice
- Appropriate level of discrimination

The question effectively tests understanding of current practice guidelines and clinical reasoning.
"""


def get_mock_ai_pal_answer() -> str:
    """
    Generate a mock AI-Pal answer with HTML formatting.
    
    Returns:
        str: HTML-formatted answer text
    """
    return """
        <div class="explanation-wrapper">
            <section class="explanation-section ai-pal-section">
                <div class="section-header">
                    <h2><i class="bi bi-robot"></i> AI-Pal's Response</h2>
                </div>
                <div class="section-content">
                    <p>The key to understanding this concept is recognizing the neuroanatomical pathway involved. The symptoms describe a classic presentation where the lesion affects the specific tract responsible for these deficits.</p>
                    
                    <h3 class="subsection-header"><i class="bi bi-arrow-right-circle"></i> Neuroanatomical Considerations</h3>
                    
                    <p>When examining these symptoms, consider:</p>
                    <ul class="custom-ul">
                        <li class="bullet-item"><i class="bi bi-dash"></i> The pathway from cortex to end organ</li>
                        <li class="bullet-item"><i class="bi bi-dash"></i> Crossing vs. non-crossing fibers</li>
                        <li class="bullet-item"><i class="bi bi-dash"></i> Upper vs. lower motor neuron involvement</li>
                    </ul>
                    
                    <h3 class="subsection-header"><i class="bi bi-arrow-right-circle"></i> Clinical Correlation</h3>
                    
                    <p>The correct answer (<span class="option-badge option-B">B</span> Option B) is supported by the latest evidence from the American Academy of Neurology guidelines (2024), which identified this pattern as highly specific (>95%) for the diagnosis.</p>
                    
                    <div class="table-card card shadow-sm mb-4"><div class="table-responsive">
                        <table class="table table-striped table-hover table-bordered mb-0">
                            <thead class="table-primary"><tr>
                                <th>Finding</th>
                                <th>Sensitivity</th>
                                <th>Specificity</th>
                                <th>Implication</th>
                            </tr></thead>
                            <tbody>
                                <tr class="table-light">
                                    <td>Key symptom #1</td>
                                    <td>80%</td>
                                    <td>70%</td>
                                    <td>Suggestive but not diagnostic</td>
                                </tr>
                                <tr>
                                    <td>Key symptom #2</td>
                                    <td>95%</td>
                                    <td>90%</td>
                                    <td>Highly indicative</td>
                                </tr>
                                <tr class="table-light">
                                    <td>Combined pattern</td>
                                    <td>92%</td>
                                    <td>97%</td>
                                    <td>Virtually diagnostic</td>
                                </tr>
                            </tbody>
                        </table>
                    </div></div>
                    
                    <div class="reference-box">
                        <div class="reference-header"><i class="bi bi-journal-text"></i> References:</div>
                        <div class="reference-content">
                            1. Smith et al. (2024). Diagnostic approach to neurological presentations. Neurology, 96(3), 234-245.
                            2. American Academy of Neurology Guidelines (2024). Evidence-based diagnostic criteria.
                        </div>
                    </div>
                    
                    <div class="text-muted small text-end mt-3">
                        <em>Generated with AI assistance</em>
                    </div>
                </div>
            </section>
        </div>
    """


def get_mock_reasoning_coach(is_correct: bool = False) -> str:
    """
    Generate a mock clinical reasoning coach response.
    Provides appropriate feedback based on whether the answer was correct.
    
    Args:
        is_correct: Whether the user's answer was correct
        
    Returns:
        str: Formatted coaching response
    """
    if is_correct:
        return """# Clinical Reasoning Analysis - CORRECT RESPONSE

## STRENGTHS IN YOUR REASONING
Your answer was correct and your reasoning shows good clinical judgment. You correctly identified the key features in the case presentation and applied the appropriate diagnostic criteria.

## REASONING FRAMEWORK ENHANCEMENT
While your conclusion is correct, consider strengthening your approach with this structured framework:

### Key Diagnostic Elements
- **History elements**: Focus on timing, progression, and associated symptoms
- **Physical examination**: Identify localizing neurological signs
- **Appropriate workup**: Consider these tests in this order:
  1. Basic lab studies (CBC, metabolic panel)
  2. Neuroimaging (MRI with specific sequences)
  3. Confirmatory studies (LP, electrophysiology)

## EVIDENCE-BASED FOUNDATION
The American Academy of Neurology guidelines (2024) provide Level A evidence for this approach, citing three randomized controlled trials showing improved diagnostic accuracy.

## ADVANCED PATTERN RECOGNITION
| Feature | Condition A | Condition B | Condition C |
| ------- | ---------- | ---------- | ---------- |
| Key symptom | Present | Absent | Variable |
| Age of onset | Young adult | Elderly | Any age |
| Course | Relapsing | Progressive | Acute |
| Biomarker | Positive | Negative | Variable |
| Treatment response | Excellent | Poor | Moderate |

## TAKE-HOME POINTS
- Always correlate clinical findings with neuroanatomy
- Consider differential diagnosis even when the answer seems obvious
- Follow evidence-based diagnostic algorithms
- Remember that timing and progression often narrow the differential significantly
"""
    else:
        return """# Clinical Reasoning Analysis - INCORRECT RESPONSE

## ERROR PATTERN IDENTIFIED
You selected an incorrect answer. The key error in your reasoning appears to be focusing on a rare presentation while missing the classic pattern described in the case.

## QUESTION DECONSTRUCTION
Breaking down this question:
- The **key clinical feature** was the pattern of deficits (bilateral, asymmetric)
- The **timing** strongly suggests an acquired rather than congenital process
- The **associated symptoms** point to a specific neural pathway

## EVIDENCE-BASED REASONING PATH
The correct diagnostic approach would be:
1. Recognize the pattern as characteristic of [condition]
2. Apply the diagnostic criteria (AAN Guidelines, 2024)
3. Consider the appropriate first-line investigation
4. Rule out key differential diagnoses

## COMPARATIVE ANALYSIS
| Feature | Your Selected Answer | Correct Answer |
| ------- | ------------------- | -------------- |
| Pathophysiology | Demyelinating | Vascular |
| Onset pattern | Gradual | Sudden |
| Age predilection | Younger | Older |
| Key diagnostic test | Lumbar puncture | MRI with DWI |
| Evidence strength | Level C | Level A |

## KNOWLEDGE REINFORCEMENT
To strengthen your understanding:
- Review the vascular anatomy of this region
- Practice recognizing this pattern in case scenarios
- Focus on the key distinguishing features in the history
- Remember that in board exams, the most common presentation is most likely to be tested

I recommend reviewing Chapter 14 in Adams & Victor's Principles of Neurology which covers this topic extensively.
"""

def format_options_text(mcq) -> str:
    """
    Format MCQ options for use in prompts with uniform formatting.
    Handles different MCQ object structures gracefully.
    
    Args:
        mcq: The MCQ object containing options
        
    Returns:
        str: Formatted string with options in "{option}. {text}" format
    """
    options_text = ""
    options_dict = {}
    
    # Handle different MCQ object structures
    if hasattr(mcq, 'get_options_dict') and callable(getattr(mcq, 'get_options_dict')):
        # If the MCQ has a method to get options as a dict, use it
        options_dict = mcq.get_options_dict()
    elif hasattr(mcq, 'options'):
        # Handle string-formatted options (JSON string)
        if isinstance(mcq.options, str):
            try:
                options_dict = json.loads(mcq.options)
            except json.JSONDecodeError:
                # If parsing fails, use the string directly
                return mcq.options
        # Handle dictionary-formatted options (JSON field)
        elif isinstance(mcq.options, dict):
            options_dict = mcq.options
    
    # Generate formatted options text
    for option, text in options_dict.items():
        options_text += f"{option}. {text}\n"
    
    return options_text

def generate_explanation(mcq, reason: str = '') -> str:
    """
    Generate an evidence-based explanation for an MCQ using OpenAI API.
    Produces detailed neurological explanations with current guidelines and evidence levels.
    
    Args:
        mcq: The MCQ object to explain
        reason: Optional reason for regeneration with specific focus
        
    Returns:
        str: Formatted explanation text with evidence-based analysis and tables
    """
    # Use mock explanation when API is unavailable
    if not api_key or not client:
        logger.info("Using mock explanation due to unavailable OpenAI API")
        return get_mock_explanation()
    
    try:
        logger.info(f"Generating explanation for MCQ with ID: {getattr(mcq, 'id', 'unknown')}")
        
        # Get options in standardized format
        options_text = format_options_text(mcq)
        
        # Extract individual options for the formatted prompt with enhanced error handling
        options_dict = {}
        try:
            # Get options dictionary using our standardized helper
            if hasattr(mcq, 'get_options_dict') and callable(getattr(mcq, 'get_options_dict')):
                options_dict = mcq.get_options_dict()
            elif hasattr(mcq, 'options'):
                if isinstance(mcq.options, str):
                    try:
                        options_dict = json.loads(mcq.options)
                    except json.JSONDecodeError:
                        # Parse from formatted options if JSON parsing fails
                        lines = options_text.strip().split('\n')
                        for line in lines:
                            if line and '. ' in line:
                                parts = line.split('. ', 1)
                                if len(parts) == 2:
                                    options_dict[parts[0]] = parts[1]
                elif isinstance(mcq.options, dict):
                    options_dict = mcq.options
        except Exception as opt_error:
            logger.warning(f"Error parsing options: {str(opt_error)}")
            # Fallback to empty dictionary if all parsing fails
            options_dict = {}
            
        # Format individual options with safe defaults
        option_a = options_dict.get('A', 'Not provided')
        option_b = options_dict.get('B', 'Not provided')
        option_c = options_dict.get('C', 'Not provided')
        option_d = options_dict.get('D', 'Not provided')
        option_e = options_dict.get('E', '')  # Option E might not exist
        
        # Build evidence-based expert neurology MCQ explanation prompt
        prompt = f"""# ENHANCED NEUROLOGY MCQ EXPLANATION GENERATOR 2025

## EXPERT ROLE DEFINITION
You are an internationally recognized neurology professor specializing in board examination preparation, with decades of clinical, research, and educational experience. Your expertise is in creating precise, evidence-based explanations that highlight the most current clinical guidelines and research while avoiding outdated information or misconceptions.

## MCQ CONTENT
Question: {getattr(mcq, 'question_text', 'Question not available')}
Options:
A. {option_a}
B. {option_b}
C. {option_c}
D. {option_d}
"""
        
        # Add option E if it exists
        if option_e:
            prompt += f"E. {option_e}\n"
            
        prompt += f"""
Correct answer: {getattr(mcq, 'correct_answer', '?')}

"""

        # Add specific focus if reason is provided
        if reason:
            prompt += f"""## USER-REQUESTED FOCUS
The learner has specifically requested emphasis on:
{reason}

"""

        prompt += f"""## STRUCTURED EXPLANATION FRAMEWORK

### 1. CONCEPTUAL FRAMEWORK & CLINICAL CONTEXT (15%)
- Key neurologic principle being tested in this question
- Essential clinical reasoning approach for this topic
- How this concept manifests in actual clinical practice
- Critical distinctions board examiners are assessing with this question

### 2. CORRECT ANSWER JUSTIFICATION (25%)
- Comprehensive explanation of why option {getattr(mcq, 'correct_answer', '?')} is correct
- Specific pathophysiological mechanisms involved
- Relevant neuroanatomical correlations
- Current evidence supporting this as standard of care
- Citations from 2023-2025 guidelines or landmark studies with strength of evidence levels
- For management questions: Include specific guideline recommendations from AAN, EAN, or other authoritative sources with evidence levels (A, B, C)

### 3. INCORRECT OPTIONS ANALYSIS (20%)
- Systematic breakdown of why each incorrect option is wrong
- Common cognitive biases or misconceptions each wrong answer exploits
- Specific evidence contradicting each incorrect option
- Clinical scenarios where these distractors might be considered but are suboptimal

### 4. COMPARATIVE ANALYSIS TABLE (15%)
Create a well-structured, properly formatted comparison table with these columns:
| Option | Key Concept/Mechanism | Evidence Quality | Clinical Application | Why Correct/Incorrect |

IMPORTANT FORMATTING GUIDELINES FOR THE TABLE:
- Use clean, consistent table markdown with proper alignment
- Ensure each cell contains concise, factual information (1-2 sentences)
- Include clear delineation between headers and content rows
- Make sure the table renders properly with adequate spacing
- Highlight the correct answer in the table with appropriate notation

### 5. HIGH-YIELD CLINICAL PEARLS (10%)
- 3-5 critical points essential for board exams and clinical practice
- Evidence-based learning aids (mnemonics, algorithms)
- Key distinguishing diagnostic or treatment decision points
- Common board exam variations on this topic
- Recent advances or evolving understanding in this area

### 6. CURRENT GUIDELINES & EVIDENCE SUMMARY (10%)
- Latest neurological society guidelines (prioritize AAN, EAN publications from 2023-2025)
- Specific evidence levels for key recommendations using standard A-C classification
- Areas of clinical consensus vs. controversy
- Any recent updates or practice-changing evidence
- For diagnostic topics:
  | Diagnostic Criteria | Sensitivity/Specificity | Gold Standard | Guideline Source & Year |
- For treatment topics:
  | Intervention | Recommendation Strength | Evidence Level | Key Patient Considerations | Guideline & Year |

### 7. INTEGRATED CLINICAL APPLICATION (5%)
- Concise take-home message connecting this concept to clinical practice
- How understanding this topic improves patient care
- Next steps in workup or management
- How this concept connects to broader neurological principles

## ENHANCED EVIDENCE STANDARDS
Your explanation MUST:
- Prioritize ONLY the most current guidelines (2023-2025 whenever available)
- Assign proper evidence levels (A: strong evidence from RCTs, B: moderate evidence, C: expert consensus)
- Explicitly distinguish between established facts and areas of uncertainty
- Cite specific references for ALL major claims (Author/Organization, Publication, Year)
- Use the most recent evidence-based diagnostic criteria and management approaches
- Acknowledge limitations of evidence where appropriate

## PRECISE FORMATTING REQUIREMENTS
- Use consistent header formatting and hierarchical structure
- Format all tables with proper markdown syntax and alignment
- Present references in standard academic format
- Use bullet points consistently with proper indentation
- Bold key concepts, diagnostic criteria, and important distinctions
- Ensure proper spacing and alignment throughout

Always prioritize clinical accuracy over comprehensiveness. When evidence conflicts, explicitly state which sources are considered most authoritative and why.
"""
        
        # Define model and API parameters
        model = DEFAULT_MODEL
        max_tokens = 4000
        temperature = 0.2
        timeout = 120
        
        # Use the OpenAI API with strategic retry handling
        start_time = time.time()
        try:
            logger.info(f"Calling OpenAI API with model: {model}")
            response = chat_completion(
                client,
                model,
                [
                    {"role": "system", "content": "You are a senior neurology professor creating evidence-based explanations for board exam preparation. Prioritize clinical accuracy, current guidelines, and educational value."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=0.95,
                frequency_penalty=0.1,
                presence_penalty=0.1,
                timeout=timeout,
            )
            duration = time.time() - start_time
            logger.info(f"OpenAI API call successful. Duration: {duration:.2f}s")
            
            content = ''
            try:
                content = get_first_choice_text(response) if response else ''
            except Exception:
                content = ''
            if not content or not str(content).strip():
                try:
                    fb_resp = chat_completion(
                        client,
                        FALLBACK_MODEL,
                        [
                            {"role": "system", "content": "You are a senior neurology professor creating evidence-based explanations for board exam preparation. Prioritize clinical accuracy, current guidelines, and educational value."},
                            {"role": "user", "content": prompt}
                        ],
                        max_tokens=1500,
                        timeout=60
                    )
                    content = get_first_choice_text(fb_resp) if fb_resp else ''
                except Exception:
                    content = ''
            return content or "# EXPLANATION GENERATION\n\nUnable to generate a detailed explanation right now. Please try again."
            
        except Exception as primary_error:
            # Log detailed error information
            error_type = type(primary_error).__name__
            error_msg = str(primary_error)
            logger.warning(f"Primary API error ({error_type}): {error_msg}")
            
            # Adjust parameters for retry
            retry_model = model
            # If the error suggests an invalid/unknown model, fall back to a safe mini model
            try:
                lowered = (error_msg or "").lower()
                if any(term in lowered for term in [
                    "invalid model", "unknown model", "model not found", "does not exist", "no such model"
                ]):
                    logger.warning(f"Model '{model}' appears unavailable. Falling back to '{FALLBACK_MODEL}'.")
                    retry_model = FALLBACK_MODEL
            except Exception:
                pass
            retry_max_tokens = 2500
            retry_temperature = 0.3
            retry_timeout = 90
            
            # Retry with adjusted parameters
            try:
                logger.info(f"Retrying with adjusted parameters: tokens={retry_max_tokens}, temp={retry_temperature}")
                response = chat_completion(
                    client,
                    retry_model,
                    [
                        {"role": "system", "content": "You are a senior neurology professor creating evidence-based explanations for board exam preparation. Prioritize clinical accuracy and educational value."},
                        {"role": "user", "content": prompt}
                    ],
                    max_tokens=retry_max_tokens,
                    temperature=retry_temperature,
                    timeout=retry_timeout
                )
                
                retry_duration = time.time() - start_time
                logger.info(f"Retry successful. Total duration: {retry_duration:.2f}s")
                
                content = ''
                try:
                    content = get_first_choice_text(response) if response else ''
                except Exception:
                    content = ''
                return content or "Explanation temporarily unavailable. Please try again."
                
            except Exception as retry_error:
                # If retry also fails, log and return error
                retry_error_type = type(retry_error).__name__
                retry_error_msg = str(retry_error)
                logger.error(f"API retry also failed ({retry_error_type}): {retry_error_msg}")
                
                # Return a more user-friendly error message with appropriate fallback content
                return f"""# EXPLANATION GENERATION ERROR

Unfortunately, an error occurred while generating the explanation. The system was unable to complete your request due to an API service issue.

## Technical Details
- Primary error: {error_type} - {error_msg}
- Secondary error: {retry_error_type} - {retry_error_msg}

## Basic Information
- The correct answer is option {getattr(mcq, 'correct_answer', '?')}.
- Please try again later when the service is available.

{get_mock_explanation()}
"""
    
    except Exception as e:
        # Log unexpected errors in the overall process
        logger.error(f"Unexpected error in generate_explanation: {str(e)}")
        return f"Error generating explanation: {str(e)}"

def improve_question(mcq) -> str:
    """
    Improve an MCQ by rephrasing for clarity without altering medical content.
    Enhances readability while maintaining all clinical details and testing focus.
    
    Args:
        mcq: The MCQ object containing the question to improve
        
    Returns:
        str: Improved question text or error message
    """
    # Return appropriate message when API is unavailable
    if not api_key or not client:
        logger.info("Question improvement skipped due to unavailable OpenAI API")
        return get_mock_improved_question(getattr(mcq, 'question_text', ''))
    
    try:
        question_text = getattr(mcq, 'question_text', '')
        if not question_text:
            return "No question text provided."
            
        logger.info(f"Improving question for MCQ with ID: {getattr(mcq, 'id', 'unknown')}")
        
        # Define precise system and user prompts for consistent improvement
        system_prompt = """You are a neurology editor who helps clarify and improve the phrasing of MCQ questions.
        
Your role is STRICTLY LIMITED to:
1. Improving grammar and sentence structure
2. Clarifying ambiguous wording
3. Simplifying complex language while preserving all medical terms
4. Ensuring consistent terminology
5. Formatting the question for better readability

You MUST follow these strict guidelines:
- DO NOT add any new clinical details, symptoms, or findings not present in the original
- DO NOT change any medical facts, terms, or conditions mentioned
- DO NOT alter the fundamental scenario being described
- DO NOT change the difficulty level or what knowledge is being tested
- PRESERVE all key clinical details exactly as presented

Your goal is better phrasing and clarity ONLY, not new content.
"""
        
        user_prompt = f"""Here is a neurology MCQ question that needs rephrasing for clarity:

ORIGINAL QUESTION: {question_text}

I need you to rephrase this question to improve clarity and readability. The question tests the correct concept, but the wording could be clearer.

Your task:
1. Fix any grammatical issues or awkward phrasing
2. Improve clarity and precision of language
3. Ensure the question is structured logically
4. Preserve ALL medical details and the clinical scenario exactly as presented

IMPORTANT: Return ONLY the rephrased question. DO NOT explain your changes or include any other text in your response.
"""
        
        # Configure consistent API parameters
        model = DEFAULT_MODEL
        max_tokens = 1000
        temperature = 0.2
        
        # Track API call timing for performance monitoring
        start_time = time.time()
        
        try:
            # First attempt with optimal parameters
            logger.info(f"Calling OpenAI API with model: {model}")
            response = chat_completion(
                client,
                model,
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=0.95,
                frequency_penalty=0.1
            )
            
            duration = time.time() - start_time
            logger.info(f"Question improvement successful. Duration: {duration:.2f}s")
            
            improved_question = get_first_choice_text(response).strip()
            
        except Exception as primary_error:
            # Log the primary error details
            error_type = type(primary_error).__name__
            error_msg = str(primary_error)
            logger.warning(f"Primary API error in improve_question ({error_type}): {error_msg}")
            
            # Retry with more conservative parameters
            try:
                logger.info("Retrying question improvement with conservative parameters")
                response = chat_completion(
                    client,
                    model,
                    [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    max_tokens=500,
                    temperature=0.3  # Higher temperature for more conservative changes
                )
                
                retry_duration = time.time() - start_time
                logger.info(f"Question improvement retry successful. Total duration: {retry_duration:.2f}s")
                
                improved_question = get_first_choice_text(response).strip()
                
            except Exception as retry_error:
                # If retry fails, log and return the original question
                retry_error_type = type(retry_error).__name__
                retry_error_msg = str(retry_error)
                logger.error(f"Question improvement retry failed ({retry_error_type}): {retry_error_msg}")
                
                # Return the original question with error context
                return question_text
        
        # Process the model's response to extract only the improved question
        if "REPHRASED QUESTION:" in improved_question:
            improved_question = improved_question.split("REPHRASED QUESTION:")[1].strip()
        
        # Remove any additional explanations or notes
        if "\n\n" in improved_question:
            improved_question = improved_question.split("\n\n")[0].strip()
        
        # Validate the improved question length to ensure it's reasonable
        original_length = len(question_text)
        improved_length = len(improved_question)
        
        # Reject improvement if length differs significantly (> 30%)
        if improved_length < original_length * 0.7 or improved_length > original_length * 1.3:
            logger.warning(f"Rejected question improvement: length changed by {(improved_length / original_length - 1) * 100:.1f}%")
            return question_text
        
        logger.info("Question successfully improved")
        return improved_question
        
    except Exception as e:
        # Log unexpected errors in the improvement process
        logger.error(f"Unexpected error in improve_question: {str(e)}")
        return question_text

def generate_new_options(mcq) -> Dict[str, str]:
    """
    Generate new distractor options for an MCQ using OpenAI.
    Creates medically accurate options while preserving the correct answer.
    
    Args:
        mcq: The MCQ object to generate new options for
        
    Returns:
        Dict[str, str]: Dictionary mapping option letters to option text
    """
    # Get the correct answer and question text with proper error handling
    correct_letter = getattr(mcq, 'correct_answer', 'A')
    question_text = getattr(mcq, 'question_text', '')
    
    # Use mock options when API is unavailable
    if not api_key or not client:
        logger.info("Using mock options due to unavailable OpenAI API")
        mock_options = get_mock_options()
        
        # Try to preserve the original correct answer option if available
        try:
            existing_options = None
            
            # Extract existing options based on data type
            if hasattr(mcq, 'options'):
                if isinstance(mcq.options, str):
                    try:
                        existing_options = json.loads(mcq.options)
                    except json.JSONDecodeError:
                        pass
                elif isinstance(mcq.options, dict):
                    existing_options = mcq.options
                
                # Use the current correct option if available
                if existing_options and correct_letter in existing_options:
                    correct_option_text = existing_options[correct_letter]
                    mock_options[correct_letter] = correct_option_text
        except Exception as mock_error:
            logger.warning(f"Error customizing mock options: {str(mock_error)}")
            # Keep default mock options if customization fails
            
        return mock_options
    
    try:
        logger.info(f"Generating new options for MCQ with ID: {getattr(mcq, 'id', 'unknown')}")
        
        # Create educational expert prompt for option generation
        system_prompt = """You are a board-certified neurologist and medical education specialist with expertise in crafting high-quality MCQ options.

Your expertise includes:
1. Creating plausible distractors that test critical thinking
2. Ensuring medical accuracy in all options
3. Maintaining appropriate difficulty level for board exam preparation
4. Crafting options that discriminate between levels of understanding
5. Following best practices in multiple-choice question design

All options should be:
- Medically accurate and evidence-based
- Similar in length, style, and complexity
- Free of grammatical cues that hint at the answer
- Focused on testing understanding rather than recall
- Constructed to avoid "all/none of the above" patterns
"""
        
        user_prompt = f"""Question: {question_text}

The correct answer should be option {correct_letter}.

Task: Generate 5 plausible options labeled A through E, where option {correct_letter} is correct.
- Make the correct answer medically accurate and evidence-based
- Create distractors that represent common misconceptions
- Ensure all options are similar in length and structure
- Make all options medically plausible (no obviously wrong answers)
- Avoid grammatical cues that hint at the answer

Format your response as a JSON object with option letters as keys and option text as values.
Example format: {{"A": "Option text", "B": "Option text", ...}}

IMPORTANT: Return ONLY the JSON object with no additional explanation.
"""
        
        # Define API parameters
        model = DEFAULT_MODEL
        max_tokens = 1000
        temperature = 0.4  # Balanced for creativity and accuracy
        
        # Track API call timing
        start_time = time.time()
        
        try:
            # Primary API call with optimal parameters
            logger.info(f"Calling OpenAI API to generate options with model: {model}")
            response = chat_completion(
                client,
                model,
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=0.92,
                frequency_penalty=0.2,  # Prevent repetitive options
                presence_penalty=0.3    # Encourage diverse distractors
            )
            
            duration = time.time() - start_time
            logger.info(f"Options generation successful. Duration: {duration:.2f}s")
            
            content = get_first_choice_text(response)
            
        except Exception as primary_error:
            # Log the primary error details
            error_type = type(primary_error).__name__
            error_msg = str(primary_error)
            logger.warning(f"Primary API error in generate_new_options ({error_type}): {error_msg}")
            
            # Retry with modified parameters
            try:
                # Fallback to a safe model if the configured one seems invalid
                retry_model = model
                try:
                    lowered = (error_msg or "").lower()
                    if any(term in lowered for term in [
                        "invalid model", "unknown model", "model not found", "does not exist", "no such model"
                    ]):
                        logger.warning(f"Model '{model}' appears unavailable. Falling back to '{FALLBACK_MODEL}'.")
                        retry_model = FALLBACK_MODEL
                except Exception:
                    pass
                logger.info("Retrying options generation with modified parameters")
                response = chat_completion(
                    client,
                    retry_model,
                    [
                        {"role": "system", "content": "You are a neurology education expert generating MCQ options."},
                        {"role": "user", "content": user_prompt}
                    ],
                    max_tokens=500,
                    temperature=0.7  # Higher temperature for more flexibility in retry
                )
                
                retry_duration = time.time() - start_time
                logger.info(f"Options generation retry successful. Total duration: {retry_duration:.2f}s")
                
                content = get_first_choice_text(response)
                
            except Exception as retry_error:
                # If retry fails, use mock options but preserve correct answer
                retry_error_type = type(retry_error).__name__
                retry_error_msg = str(retry_error)
                logger.error(f"Options generation retry also failed ({retry_error_type}): {retry_error_msg}")
                
                # Use mock options with original correct answer as fallback
                return get_mock_options()
        
        # Process the API response to extract JSON
        try:
            # Extract JSON object if it's embedded in explanatory text
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if json_match:
                content = json_match.group(0)
                
            # Parse the JSON content
            new_options = json.loads(content)
            
            # Validate the returned options format
            if not isinstance(new_options, dict):
                raise ValueError("API returned non-dictionary options data")
                
            # Ensure we have required option letters (A-E)
            required_options = ['A', 'B', 'C', 'D']
            if not all(opt in new_options for opt in required_options):
                raise ValueError(f"Missing required options. Got: {list(new_options.keys())}")
                
            # Ensure the correct answer is present
            if correct_letter not in new_options:
                raise ValueError(f"Correct answer '{correct_letter}' missing from generated options")
                
            # Log success and return the new options
            logger.info(f"Successfully generated new options with {len(new_options)} choices")
            return new_options
            
        except json.JSONDecodeError as json_error:
            # Handle JSON parsing errors
            logger.error(f"Failed to parse options JSON: {str(json_error)}")
            logger.error(f"Raw content: {content[:100]}...")
            
            # Return error status in structured format
            return {
                "A": "Error: Could not generate options (JSON parsing failed)",
                "B": "Please try again later",
                "C": "Or use the original options",
                "D": "Contact support if this issue persists",
                "E": f"Error details: {str(json_error)[:50]}..."
            }
            
    except Exception as e:
        # Log unexpected errors in the overall process
        logger.error(f"Unexpected error in generate_new_options: {str(e)}")
        
        # Return error indication in a usable format
        return {
            "error": f"Error generating options: {str(e)}",
            "A": "Option generation failed",
            "B": "Please try again later",
            "C": "Or use the original options",
            "D": "Contact support if this issue persists",
            "E": "Technical error in option generation"
        }

def verify_mcq_answer(mcq) -> str:
    """
    Verify the correct answer of an MCQ using OpenAI API.
    Provides evidence-based verification using current neurological guidelines.
    
    Args:
        mcq: The MCQ object containing the question and answers to verify
        
    Returns:
        str: Formatted verification report with evidence-based analysis
    """
    # Get the correct answer with proper error handling
    correct_answer = getattr(mcq, 'correct_answer', 'B')
    
    # Return customized mock verification when API is unavailable
    if not api_key or not client:
        logger.info("Using mock verification due to unavailable OpenAI API")
        
        try:
            # Get mock verification template
            mock_verification = get_mock_verification()
            
            # Customize the mock verification with the correct answer
            mock_verification = mock_verification.replace("Option B", f"Option {correct_answer}")
            
            # Replace specific options in the incorrect options analysis
            incorrect_options_section = "## Incorrect Options Analysis ❌"
            if incorrect_options_section in mock_verification:
                options = ["A", "B", "C", "D", "E"]
                incorrect_options = [opt for opt in options if opt != correct_answer and opt in "ABCDE"]
                
                # Get the section content
                section_start = mock_verification.find(incorrect_options_section)
                next_section = mock_verification.find("##", section_start + len(incorrect_options_section))
                if next_section == -1:
                    next_section = len(mock_verification)
                
                incorrect_section = mock_verification[section_start:next_section]
                
                # Replace generic incorrect options with actual options
                new_section = incorrect_options_section + "\n"
                lines = incorrect_section.split("\n")[1:]  # Skip header
                
                for i, line in enumerate(lines):
                    if i < len(incorrect_options):
                        # Replace generic option with actual incorrect option
                        colon_pos = line.find(':')
                        if colon_pos != -1:
                            new_section += f"- Option {incorrect_options[i]}{line[colon_pos:]}\n"
                        else:
                            new_section += f"- Option {incorrect_options[i]}: This option is incorrect\n"
                    else:
                        new_section += line + "\n"
                
                # Replace the section in the mock verification
                mock_verification = mock_verification[:section_start] + new_section + mock_verification[next_section:]
            
            return mock_verification
            
        except Exception as mock_error:
            # If customization fails, return the default mock verification
            logger.warning(f"Error customizing mock verification: {str(mock_error)}")
            return get_mock_verification()
    
    try:
        logger.info(f"Verifying answer for MCQ with ID: {getattr(mcq, 'id', 'unknown')}")
        
        # Get options in standardized format with error handling
        options_text = format_options_text(mcq)
        
        # Extract individual options with robust error handling
        options_dict = {}
        try:
            # Get options dictionary using standardized helper
            if hasattr(mcq, 'get_options_dict') and callable(getattr(mcq, 'get_options_dict')):
                options_dict = mcq.get_options_dict()
            elif hasattr(mcq, 'options'):
                if isinstance(mcq.options, str):
                    try:
                        options_dict = json.loads(mcq.options)
                    except json.JSONDecodeError:
                        # If JSON parsing fails, keep empty dictionary
                        pass
                elif isinstance(mcq.options, dict):
                    options_dict = mcq.options
        except Exception as opt_error:
            logger.warning(f"Error parsing options for verification: {str(opt_error)}")
        
        # Format individual options with safe defaults
        option_a = options_dict.get('A', 'Not provided')
        option_b = options_dict.get('B', 'Not provided')
        option_c = options_dict.get('C', 'Not provided')
        option_d = options_dict.get('D', 'Not provided')
        option_e = options_dict.get('E', '')  # Option E might not exist
        
        # Construct evidence-based verification prompt
        prompt = f"""# NEUROLOGY MCQ VERIFICATION EXPERT

## ROLE AND EXPERTISE
You are a board-certified neurologist with expertise in evidence-based medicine tasked with verifying multiple-choice questions (MCQs) for accuracy. You have extensive clinical experience and in-depth knowledge of current neurological guidelines, landmark studies, and best practices.

## MCQ DETAILS
Question: {getattr(mcq, 'question_text', 'Question not available')}
Options:
A. {option_a}
B. {option_b}
C. {option_c}
D. {option_d}
"""
        
        # Add option E if it exists
        if option_e:
            prompt += f"E. {option_e}\n"
            
        prompt += f"""
Currently marked correct answer: {correct_answer}

## VERIFICATION PROCESS
Please follow this structured approach in your assessment:

1. EVIDENCE SYNTHESIS (30%)
   - Review current authoritative guidelines on this topic (e.g., AAN, EAN, WFN)
   - Identify relevant landmark studies or meta-analyses
   - Consider strength of evidence (Level I-IV) for each potential answer

2. CORRECT ANSWER VERIFICATION (30%)
   - Evaluate whether the marked answer aligns with current best evidence
   - Cite specific guideline recommendations or study findings supporting/refuting the marked answer
   - If a different answer is correct, clearly state which one with evidence-based justification

3. ANALYSIS OF INCORRECT OPTIONS (25%)
   - Briefly explain why each incorrect option is wrong
   - Note common misconceptions that might lead to selecting incorrect options
   - Identify if any distractors are partially correct but not the best answer

4. QUALITY ASSESSMENT (15%)
   - Evaluate the quality of the question (clarity, specificity, clinical relevance)
   - Identify any ambiguities or outdated information
   - Suggest improvements if the question has structural issues

## RESPONSE FORMAT
1. Evidence Summary: Synthesize current guidelines and key studies relevant to this question.
2. Correct Answer: [Option letter] with detailed justification and specific citations.
3. Incorrect Options: Brief explanation of why each incorrect option is wrong.
4. Question Quality: Assessment of the question's clarity, relevance, and construction.
5. Improvement Suggestions: If applicable.

Always prioritize evidence in this order:
1. Current clinical practice guidelines (published within 5 years)
2. Systematic reviews and meta-analyses
3. Large randomized controlled trials
4. Cohort studies or case-control studies
5. Expert consensus when higher-level evidence is unavailable

Format your response with appropriate headings, bold text for key points, and clear organization. Use emoji icons where appropriate to improve readability (like 📊 for evidence, ✅ for correct answers, ❌ for incorrect options, etc.).

Your verification must be precise, objective, and solely based on current medical evidence.
"""

        # Define API parameters
        model = DEFAULT_MODEL
        max_tokens = 4000
        temperature = 0.1  # Very low temperature for maximum factual accuracy
        
        # Track API call timing
        start_time = time.time()
        
        try:
            # Primary API call with optimal parameters
            logger.info(f"Calling OpenAI API for answer verification with model: {model}")
            response = chat_completion(
                client,
                model,
                [
                    {"role": "system", "content": "You are a board-certified neurologist and clinical researcher specializing in evidence-based verification of neurological concepts and treatments. Your expertise includes critically evaluating medical claims against the most recent literature (2023-2025) and current practice guidelines. You rigorously assess evidence quality using standardized classification systems (Level A/B/C evidence). Your responses are meticulously formatted with proper hierarchical structure, perfectly aligned tables, consistent bullet formatting, and appropriate visual organization. You excel at creating comparison tables that highlight key differences between correct and incorrect options. When citing guidelines or studies, you always include publication year, organization, and evidence level. You maintain complete factual accuracy while being methodical and comprehensive in your analysis."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=0.98,
                frequency_penalty=0.0,  # No penalty for repetition of important facts
                presence_penalty=0.1     # Slight penalty to avoid topic drift
            )
            
            duration = time.time() - start_time
            logger.info(f"Answer verification successful. Duration: {duration:.2f}s")
            
            txt = ''
            try:
                txt = get_first_choice_text(response) if response else ''
            except Exception:
                txt = ''
            if not txt or not str(txt).strip():
                # Attempt fallback model
                try:
                    fb = chat_completion(
                        client,
                        FALLBACK_MODEL,
                        [
                            {"role": "system", "content": "You are a neurology expert verifying MCQ answers based on evidence-based medicine."},
                            {"role": "user", "content": prompt}
                        ],
                        max_tokens=1500,
                        timeout=40
                    )
                    txt = get_first_choice_text(fb) if fb else ''
                except Exception:
                    txt = ''
            return txt or "# VERIFICATION\n\nUnable to produce verification right now. Please retry."
            
        except Exception as primary_error:
            # Log the primary error details
            error_type = type(primary_error).__name__
            error_msg = str(primary_error)
            logger.warning(f"Primary API error in verify_mcq_answer ({error_type}): {error_msg}")
            
            # Retry with more conservative parameters
            try:
                logger.info("Retrying answer verification with conservative parameters")
                response = chat_completion(
                    client,
                    model,  # Use the same model
                    [
                        {"role": "system", "content": "You are a neurology expert verifying MCQ answers based on evidence-based medicine. Format your response professionally with appropriate headings and icons."},
                        {"role": "user", "content": prompt}
                    ],
                    max_tokens=2000,  # Reduced token count
                    temperature=0.3    # Slightly higher temperature
                )
                
                retry_duration = time.time() - start_time
                logger.info(f"Answer verification retry successful. Total duration: {retry_duration:.2f}s")
                
                txt2 = ''
                try:
                    txt2 = get_first_choice_text(response) if response else ''
                except Exception:
                    txt2 = ''
                return txt2 or "Verification temporarily unavailable."
                
            except Exception as retry_error:
                # If retry also fails, fall back to mock verification
                retry_error_type = type(retry_error).__name__
                retry_error_msg = str(retry_error)
                logger.error(f"Answer verification retry failed ({retry_error_type}): {retry_error_msg}")
                
                # Provide a customized error response with fallback verification
                custom_error_msg = f"""# VERIFICATION ERROR

Unfortunately, we were unable to verify this MCQ answer at this time due to a technical issue.

## Error Details
- Primary error: {error_type} - {error_msg}
- Retry error: {retry_error_type} - {retry_error_msg}

## Basic Verification
Without API access, we can confirm that option {correct_answer} is marked as the correct answer in our database.

{get_mock_verification().replace("Option B", f"Option {correct_answer}")}
"""
                return custom_error_msg
    
    except Exception as e:
        # Log unexpected errors in the overall process
        logger.error(f"Unexpected error in verify_mcq_answer: {str(e)}")
        return f"Error verifying answer: {str(e)}"

def answer_question_about_mcq(mcq, question: str) -> str:
    """
    Answer a specific question about an MCQ using OpenAI with educational focus.
    Generates formatted HTML responses with evidence-based neurological explanations.
    Uses the standard gpt-5-mini model (or the configured fallback).
    
    Args:
        mcq: The MCQ object containing question and answer data
        question: The specific question to answer about the MCQ
        
    Returns:
        str: HTML-formatted educational response focused on the specific question
    """
    # Use mock AI-Pal response when API is unavailable
    if not api_key or not client:
        logger.info("Using mock AI-Pal response due to unavailable OpenAI API")
        return get_mock_ai_pal_answer()
    
    try:
        logger.info(f"Answering question for MCQ with ID: {getattr(mcq, 'id', 'unknown')}")
        logger.info(f"User question: {question[:100]}...")
        
        # Get options and context in standardized format
        options_text = format_options_text(mcq)
        question_text = getattr(mcq, 'question_text', 'Question not available')
        correct_answer = getattr(mcq, 'correct_answer', 'Not specified')
        
        # Complex system prompt for the Ask AI-Pal feature with gpt-5-mini
        system_prompt = """You are AI-Pal, a world-class neurology educator with fellowship training in medical education and expertise in board examination preparation.

## YOUR UNIQUE CAPABILITIES:
1. You provide evidence-based answers grounded in the most current neurological guidelines (2023-2025)
2. You explain complex neurological concepts with exceptional clarity and precision
3. You recognize and clarify common misconceptions in neurological understanding
4. You identify the core testable concepts in neurological MCQs
5. You create perfectly formatted, visually organized responses that enhance learning
6. You excel at creating structured comparisons between similar conditions or concepts
7. You always include evidence levels (A/B/C) for treatment recommendations
8. You prioritize the most reliable information sources (AAN, EAN, etc.)

## RESPONDING FORMAT:
1. Begin with a direct, concise answer to the specific question asked
2. Organize information with clear hierarchical structure using proper markdown formatting
3. Use **bold text** for critical concepts, diagnostic criteria, and key distinctions
4. Create well-structured tables with proper alignment for comparing options or conditions
5. Format lists consistently with appropriate bullet or number styles
6. Use clinical examples to illustrate abstract concepts
7. Include recent evidence citations with year and source for all major claims
8. Structure complex information with clear section headers (###)
9. DO NOT include separate sections explaining correct/incorrect answer options

## INFORMATION QUALITY:
1. Prioritize 2023-2025 guidelines and recent evidence over older sources
2. Explicitly note evidence levels for treatment recommendations (A/B/C)
3. Clearly distinguish between established facts and areas of evolving understanding
4. Address common misconceptions directly
5. Present balanced information on areas of clinical debate
6. Acknowledge limitations in current evidence when appropriate

## EDUCATIONAL APPROACH:
1. Connect pathophysiology to clinical presentation and management
2. Explain WHY rather than just WHAT
3. Highlight key decision points in clinical reasoning
4. Provide relevant pattern recognition tools for similar presentations
5. Include high-yield board examination perspectives related to the topic

Always maintain visual organization, perfect table alignment, and consistent formatting to enhance readability and learning.
"""
        
        # Modified user prompt without explanation redundancy
        user_prompt = f"""I'm studying this neurology MCQ:

Question: {question_text}

Answer Options:
{options_text}

Correct Answer: {correct_answer}

My specific question about this MCQ is: {question}

Please help me understand this with a focused, evidence-based explanation. I already have access to the general explanation of the MCQ and correct/incorrect answers, so please focus directly on my specific question.

Please structure your answer with clear sections and proper formatting for readability:
1. Start with a direct answer to my question
2. Include relevant scientific reasoning and evidence with citations to recent guidelines (2023-2025) where applicable
3. DO NOT include a section explaining why the correct answer is right or why wrong answers are wrong
4. DO NOT include a separate section connecting your answer to the MCQ - instead, integrate relevant aspects throughout your answer

Note that my question might not be directly related to the MCQ content, but you should still answer with appropriate context.
"""
        
        # Define API parameters - tuned for Heroku 30s limit
        model = DEFAULT_MODEL
        max_tokens = 1000  # Keep responses under 30s for web dyno
        temperature = 0.3  # Ignored by GPT-5 models via wrapper
        
        # Track API call timing
        start_time = time.time()
        
        # Content generation with error handling and retry logic
        try:
            # Primary API call with optimal parameters
            logger.info(f"Calling OpenAI API for AI-Pal answer with model: {model}")
            response = chat_completion(
                client,
                model,
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=0.95,
                presence_penalty=0.1,  # Slightly encourage new points
                frequency_penalty=0.2,  # Discourage repetition but allow emphasis
                timeout=25
            )
            
            duration = time.time() - start_time
            logger.info(f"AI-Pal answer generation successful. Duration: {duration:.2f}s")
            
            raw_answer = ''
            try:
                raw_answer = get_first_choice_text(response) if response else ''
            except Exception:
                raw_answer = ''

            # Handle rare case of empty content from the API by retrying with fallback model
            if not raw_answer or not str(raw_answer).strip():
                logger.warning("AI-Pal returned empty content; retrying with fallback model")
                try:
                    fallback_resp = chat_completion(
                        client,
                        FALLBACK_MODEL,
                        [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt}
                        ],
                        max_tokens=600,
                        temperature=0.4,
                        timeout=20
                    )
                    raw_answer = get_first_choice_text(fallback_resp) if fallback_resp else ''
                except Exception as fb_err:
                    logger.error(f"Fallback model also failed to produce content: {fb_err}")
                    raw_answer = ''
            
        except Exception as primary_error:
            # Log the primary error details
            error_type = type(primary_error).__name__
            error_msg = str(primary_error)
            logger.warning(f"Primary API error in answer_question_about_mcq ({error_type}): {error_msg}")
            
            # Retry with more conservative parameters
            try:
                # Fall back to a safe model if configured one seems invalid
                retry_model = model
                try:
                    lowered = (error_msg or "").lower()
                    if any(term in lowered for term in [
                        "invalid model", "unknown model", "model not found", "does not exist", "no such model"
                    ]):
                        logger.warning(f"Model '{model}' appears unavailable. Falling back to '{FALLBACK_MODEL}'.")
                        retry_model = FALLBACK_MODEL
                except Exception:
                    pass
                logger.info("Retrying AI-Pal answer with conservative parameters")
                response = chat_completion(
                    client,
                    retry_model,
                    [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    max_tokens=600,  # Further reduced token count
                    temperature=0.4,
                    timeout=20
                )
                
                retry_duration = time.time() - start_time
                logger.info(f"AI-Pal answer retry successful. Total duration: {retry_duration:.2f}s")
                
                raw_answer = get_first_choice_text(response)
                
            except Exception as retry_error:
                # If retry also fails, return a graceful error message
                retry_error_type = type(retry_error).__name__
                retry_error_msg = str(retry_error)
                logger.error(f"AI-Pal answer retry failed ({retry_error_type}): {retry_error_msg}")
                
                # Return user-friendly error with fallback content
                return f"""
                <div class="explanation-wrapper">
                    <section class="explanation-section ai-pal-section">
                        <div class="section-header">
                            <h2><i class="bi bi-robot"></i> AI-Pal's Response</h2>
                        </div>
                        <div class="section-content">
                            <div class="alert alert-warning mb-4">
                                <i class="bi bi-exclamation-triangle me-2"></i> 
                                <strong>I'm unable to answer your question right now due to a technical issue.</strong>
                                <hr>
                                <p>Here's what we know about this MCQ:</p>
                                <ul>
                                    <li>The correct answer is option {correct_answer}</li>
                                    <li>Your question was: "{question}"</li>
                                </ul>
                                <p>Please try again later when our AI service is available.</p>
                            </div>
                            
                            {get_mock_ai_pal_answer()}
                        </div>
                    </section>
                </div>
                """
        
        # Process the raw response for proper HTML formatting
        processed_answer = raw_answer or ''

        # If still empty, provide a graceful educational fallback
        if not processed_answer.strip():
            processed_answer = """
            <div class="alert alert-warning">
                <strong>Heads up:</strong> I couldn't generate a detailed answer just now. Here's a quick educational summary while I retry next time.
            </div>
            """ + get_mock_ai_pal_answer()
        
        # Convert markdown-style headers to HTML with icons
        processed_answer = re.sub(r'### (.*?)(\n|$)', r'<h3 class="subsection-header"><i class="bi bi-arrow-right-circle"></i> \1</h3>', processed_answer)
        processed_answer = re.sub(r'## (.*?)(\n|$)', r'<h3 class="subsection-header"><i class="bi bi-arrow-right-circle"></i> \1</h3>', processed_answer)
        processed_answer = re.sub(r'# (.*?)(\n|$)', r'<h2 class="section-title"><i class="bi bi-book"></i> \1</h2>', processed_answer)
        
        # Convert markdown text formatting
        processed_answer = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', processed_answer)
        processed_answer = re.sub(r'\*(.*?)\*', r'<em>\1</em>', processed_answer)
        
        # Add option badges for visual clarity
        for option in ['A', 'B', 'C', 'D', 'E']:
            # Match options that aren't part of words (boundary checking)
            processed_answer = re.sub(r'([^a-zA-Z0-9])(Option ' + option + r')([^a-zA-Z0-9])', 
                                      r'\1<span class="option-badge option-' + option + '">' + option + '</span> Option ' + option + r'\3', 
                                      processed_answer)
            processed_answer = re.sub(r'([^a-zA-Z0-9])(option ' + option + r')([^a-zA-Z0-9])', 
                                      r'\1<span class="option-badge option-' + option + '">' + option + '</span> Option ' + option + r'\3', 
                                      processed_answer)
        
        # Format list items with icons and proper styling
        processed_answer = re.sub(r'- (.*?)(\n|$)', r'<li class="bullet-item"><i class="bi bi-dash"></i> \1</li>', processed_answer)
        processed_answer = re.sub(r'(\d+)\. (.*?)(\n|$)', r'<li class="numbered-item"><span class="number-badge">\1</span> \2</li>', processed_answer)
        
        # Wrap lists in proper container elements
        processed_answer = re.sub(r'(<li class="bullet-item">.*?</li>)+', r'<ul class="custom-ul">\g<0></ul>', processed_answer)
        processed_answer = re.sub(r'(<li class="numbered-item">.*?</li>)+', r'<ol class="custom-ol">\g<0></ol>', processed_answer)
        
        # Convert markdown tables to formatted HTML tables
        table_pattern = r'\| (.*?) \|[ \t]*\n\|([-|\s]*)\|[ \t]*\n((?:\| .*? \|[ \t]*\n)+)'
        
        def table_replace(match):
            # Extract table components
            header = match.group(1)
            headers = [h.strip() for h in header.split('|')]
            
            rows_text = match.group(3)
            rows = []
            for row in rows_text.strip().split('\n'):
                row = row.strip('|')
                cells = [cell.strip() for cell in row.split('|')]
                rows.append(cells)
            
            # Ensure consistent cell count for all rows
            max_cells = max(len(headers), *[len(row) for row in rows])
            for row in rows:
                while len(row) < max_cells:
                    row.append("")
            
            # Create HTML table with Bootstrap styling and card container
            html = '<div class="table-card card shadow-sm mb-4"><div class="table-responsive">'
            html += '<table class="table table-striped table-hover table-bordered mb-0">'
            
            # Table header
            html += '<thead class="table-primary"><tr>'
            for h in headers:
                html += f'<th>{h}</th>'
            # Add any missing columns in header
            for _ in range(max_cells - len(headers)):
                html += f'<th></th>'
            html += '</tr></thead>'
            
            # Table body with alternating row colors
            html += '<tbody>'
            for i, row in enumerate(rows):
                row_class = 'table-light' if i % 2 == 0 else ''
                html += f'<tr class="{row_class}">'
                for cell in row:
                    cell_content = cell.strip()
                    # Special formatting for option cells
                    if cell_content in ['A', 'B', 'C', 'D', 'E']:
                        html += f'<td><span class="option-badge option-{cell_content}">{cell_content}</span></td>'
                    # Special formatting for yes/no and correct/incorrect cells
                    elif any(term in cell_content.lower() for term in ['correct', 'yes']):
                        html += f'<td><span class="badge bg-success">{cell_content}</span></td>'
                    elif any(term in cell_content.lower() for term in ['incorrect', 'no']):
                        html += f'<td><span class="badge bg-danger">{cell_content}</span></td>'
                    else:
                        # Default formatting with proper handling of bolded text
                        cell_content = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', cell_content)
                        html += f'<td>{cell_content}</td>'
                html += '</tr>'
            html += '</tbody></table></div></div>'
            
            return html
        
        # Apply table replacement
        processed_answer = re.sub(table_pattern, table_replace, processed_answer)
        
        # Format references and citations in a styled box
        if any(term in processed_answer for term in ["Reference", "reference", "References", "references"]):
            processed_answer = re.sub(r'Reference[s]?:?([\s\S]*?)($|\n\n)', 
                          r'<div class="reference-box"><div class="reference-header"><i class="bi bi-journal-text"></i> References:</div><div class="reference-content">\1</div></div>', 
                          processed_answer)
        
        # Wrap the content in styled container elements
        formatted_answer = f"""
        <div class="explanation-wrapper">
            <section class="explanation-section ai-pal-section">
                <div class="section-header">
                    <h2><i class="bi bi-robot"></i> AI-Pal's Response</h2>
                </div>
                <div class="section-content">
                    {processed_answer}
                    <div class="text-muted small text-end mt-3">
                        <em>Generated using {model}</em>
                    </div>
                </div>
            </section>
        </div>
        """

        return formatted_answer
        
    except Exception as e:
        # Log unexpected errors in the overall process
        logger.error(f"Unexpected error in answer_question_about_mcq: {str(e)}")
        
        # Return a user-friendly error message in HTML format
        return f"""
        <div class="alert alert-danger">
            <i class="bi bi-exclamation-triangle me-2"></i> 
            <strong>Error answering question:</strong> {str(e)}
            <hr>
            <p>Our AI system encountered an error while processing your question. 
            Please try again later or try rephrasing your question.</p>
        </div>
        """

def determine_question_type(question_text: str, options_text: str) -> str:
    """
    Determine the neurological question type to provide specialized guidance.
    Analyzes question and options text for domain-specific keywords.
    
    Args:
        question_text: The MCQ question text
        options_text: The combined text of all answer options
        
    Returns:
        str: Identified question type (neuroanatomy, clinical_diagnosis, 
             pharmacotherapy, paraclinical, or general)
    """
    # Default to general type if inputs are empty
    if not question_text and not options_text:
        return "general"
    
    # Convert to lowercase for case-insensitive matching
    question_lower = question_text.lower() if question_text else ""
    options_lower = options_text.lower() if options_text else ""
    combined_text = question_lower + " " + options_lower
    
    # Define keyword sets for each question type
    question_type_keywords = {
        "neuroanatomy": [
            'localize', 'lesion', 'pathway', 'tract', 'nucleus', 'cortex', 'nerve', 
            'anatomical', 'structure', 'brainstem', 'cerebellum', 'basal ganglia', 
            'spinal cord', 'hemisphere', 'topography', 'neural', 'decussation',
            'fornix', 'thalamus', 'hypothalamus', 'medulla', 'pons', 'midbrain',
            'white matter', 'gray matter', 'gyrus', 'sulcus', 'fasciculus'
        ],
        
        "clinical_diagnosis": [
            'diagnose', 'diagnosis', 'symptom', 'sign', 'presentation', 'differential', 
            'clinical picture', 'syndrome', 'disease', 'disorder', 'condition', 
            'most likely', 'consistent with', 'classify', 'etiology', 'manifestation',
            'history', 'physical exam', 'presentation', 'onset', 'progression',
            'characteristic', 'pathognomonic', 'diagnostic criteria'
        ],
        
        "pharmacotherapy": [
            'medication', 'drug', 'therapy', 'treatment', 'dose', 'side effect', 
            'adverse effect', 'contraindication', 'mechanism of action', 'moa', 
            'pharmacokinetic', 'pharmacodynamic', 'indication', 'prescribe',
            'administer', 'dosage', 'therapeutic', 'toxicity', 'half-life',
            'metabolism', 'excretion', 'bioavailability', 'drug-drug interaction'
        ],
        
        "paraclinical": [
            'mri', 'ct', 'eeg', 'emg', 'nerve conduction', 'lumbar puncture', 'csf', 
            'imaging', 'scan', 'laboratory', 'test', 'investigation', 'finding', 
            'radiological', 'neurodiagnostic', 'biomarker', 'serology', 'genetics',
            'histopathology', 'biopsy', 'pet', 'spect', 'ultrasound', 'angiography',
            'electrodiagnostic', 'spectroscopy', 'pathology'
        ]
    }
    
    # Check each question type using comprehensive keyword sets
    for q_type, keywords in question_type_keywords.items():
        if any(term in combined_text for term in keywords):
            logger.debug(f"Question identified as {q_type} type")
            return q_type
    
    # If no specific type is identified, return general
    logger.debug("No specific question type identified, using general type")
    return "general"

def generate_test_for_weakness(original_question: str, original_options: dict,
                         original_correct: str, user_incorrect: str, explanation: str = '') -> dict:
    """
    Generate a new MCQ to test a weakness identified from a user's incorrect answer.
    Creates a focused question that tests the same concept in a new way.

    Args:
        original_question: The original MCQ question text
        original_options: Dictionary of original options {letter: text}
        original_correct: Letter of the original correct answer
        user_incorrect: Letter of the user's incorrect answer
        explanation: Optional explanation text for context

    Returns:
        dict: Dictionary with new question, options, correct_answer, and explanation
    """
    # Use mock response when API is unavailable
    if not api_key or not client:
        logger.info("Using mock weakness test generation due to unavailable OpenAI API")

        # Create basic mock question based on original
        mock_question = f"TEST_WEAKNESS_{original_question[:100]}..."
        mock_options = {
            'A': "Similar concept to the original, new wording",
            'B': "Common misconception related to original error",
            'C': "Correct concept related to the original question",
            'D': "Another plausible but incorrect option"
        }
        mock_correct = 'C'
        mock_explanation = "This question tests your understanding of the same concept from the original question, but approached from a different angle."

        return {
            'question': mock_question,
            'options': mock_options,
            'correct_answer': mock_correct,
            'explanation': mock_explanation
        }

    try:
        logger.info("Generating test for weakness based on incorrect answer")

        # Prepare context for the prompt
        original_correct_text = original_options.get(original_correct, "Not provided")
        user_incorrect_text = original_options.get(user_incorrect, "Not provided")

        # Create a focused prompt for generating a related test question
        prompt = f"""# NEUROLOGY WEAKNESS TESTING QUESTION GENERATOR

## CONTEXT
A learner was presented with this MCQ:

QUESTION: {original_question}

OPTIONS:
{format_options_text({"options": original_options})}

The CORRECT answer was: {original_correct} - {original_correct_text}

But the learner incorrectly chose: {user_incorrect} - {user_incorrect_text}

EXPLANATION: {explanation[:500]}...

## TASK
Generate a completely NEW test question that:
1. Tests the exact same neurological concept or principle where the user made a mistake
2. Uses a completely different clinical scenario or case presentation
3. Helps determine if the learner has addressed their specific knowledge gap
4. Is at the same difficulty level as the original
5. Features new clinical scenarios, patient data, or contexts to avoid memorization
6. Must be a DIFFERENT question than the original - do not reuse the original question

## REQUIREMENTS
1. The new question must focus precisely on the conceptual misunderstanding demonstrated by selecting option {user_incorrect}
2. Question must be entirely new, not just a rephrasing of the original
3. Create 4 answer options (A, B, C, D) that test understanding of the same concept
4. The options should be plausible and of similar length
5. Provide a detailed explanation for the correct answer that specifically addresses why the concept was misunderstood before
6. Identify which option (A-D) is correct
7. Include the tag "TEST_WEAKNESS_{{}}" somewhere in the question text

## OUTPUT FORMAT
Return a JSON object with these exact keys:
- question: The complete question text
- options: An object with keys A-D and option text as values
- correct_answer: The letter of the correct option
- explanation: Detailed explanation of the correct answer and concept

```json format
{{
  "question": "...",
  "options": {{
    "A": "...",
    "B": "...",
    "C": "...",
    "D": "..."
  }},
  "correct_answer": "X",
  "explanation": "..."
}}
```

Make sure the question specifically tests the same concept where the learner made their mistake, but presents it in a completely new clinical context.
"""

        # Define API parameters
        model = DEFAULT_MODEL
        max_tokens = 3000
        temperature = 0.7  # Higher temperature for creative question generation

        # Track API call timing
        start_time = time.time()

        try:
            # Primary API call with optimal parameters
            logger.info(f"Calling OpenAI API to generate weakness test with model: {model}")
            response = chat_completion(
                client,
                model,
                [
                    {"role": "system", "content": "You are a neurology board examination expert who specializes in creating targeted questions to test specific areas of weakness. You excel at identifying the conceptual misunderstanding behind incorrect answers and designing new questions that test the same concept from different angles. Your questions are always evidence-based, clinically relevant, and at appropriate difficulty levels for board examinations."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=0.9,
                frequency_penalty=0.3,  # Encourage creativity in question formulation
                presence_penalty=0.2     # Discourage repetition of original content
            )

            duration = time.time() - start_time
            logger.info(f"Weakness test generation successful. Duration: {duration:.2f}s")

            content = get_first_choice_text(response)

        except Exception as primary_error:
            # Log the primary error details
            error_type = type(primary_error).__name__
            error_msg = str(primary_error)
            logger.warning(f"Primary API error in generate_test_for_weakness ({error_type}): {error_msg}")

            # Retry with modified parameters
            try:
                logger.info("Retrying weakness test generation with modified parameters")
                response = chat_completion(
                    client,
                    model,
                    [
                        {"role": "system", "content": "You are a neurology educator who creates questions to test specific areas of weakness."},
                        {"role": "user", "content": prompt}
                    ],
                    max_tokens=1500,
                    temperature=0.9  # Even higher temperature for retry
                )

                retry_duration = time.time() - start_time
                logger.info(f"Weakness test generation retry successful. Total duration: {retry_duration:.2f}s")

                content = get_first_choice_text(response)

            except Exception as retry_error:
                # If retry fails, use a simple mock test instead
                retry_error_type = type(retry_error).__name__
                retry_error_msg = str(retry_error)
                logger.error(f"Weakness test generation retry failed ({retry_error_type}): {retry_error_msg}")

                # Return mock test with fallback values
                return {
                    'question': f"TEST_WEAKNESS_{original_question[:100]}...",
                    'options': {
                        'A': "Option related to the original question",
                        'B': "Another option testing the same concept",
                        'C': "Correct option based on proper understanding",
                        'D': "Common misconception similar to original error"
                    },
                    'correct_answer': 'C',
                    'explanation': "This question tests whether you've corrected your understanding of the concept from the original question."
                }

        # Process the API response to extract JSON
        try:
            # Extract JSON object if it's embedded in explanatory text
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if json_match:
                content = json_match.group(0)

            # Parse the JSON content
            result = json.loads(content)

            # Validate the returned data structure
            required_keys = ['question', 'options', 'correct_answer', 'explanation']
            if not all(key in result for key in required_keys):
                missing = [key for key in required_keys if key not in result]
                raise ValueError(f"Missing required keys in response: {missing}")

            # Ensure the question has the TEST_WEAKNESS tag
            if "TEST_WEAKNESS_" not in result['question']:
                result['question'] = f"TEST_WEAKNESS_{result['question']}"

            # Validate options and correct answer
            if not isinstance(result['options'], dict):
                raise ValueError("Options must be a dictionary")

            if not 'A' in result['options'] or not result['correct_answer'] in result['options']:
                raise ValueError("Invalid options or correct answer")

            # Return the validated result
            logger.info(f"Successfully generated new weakness test question")
            return result

        except (json.JSONDecodeError, ValueError) as format_error:
            # Handle JSON parsing and validation errors
            logger.error(f"Failed to parse test question: {str(format_error)}")
            logger.error(f"Raw content: {content[:100]}...")

            # Return fallback question with error context
            return {
                'question': f"TEST_WEAKNESS_{original_question[:50]}... (error: could not generate new question)",
                'options': {
                    'A': "This question could not be properly generated",
                    'B': "Please review the original question instead",
                    'C': "The original correct answer was " + original_correct_text,
                    'D': "Error details: " + str(format_error)[:50]
                },
                'correct_answer': 'C',
                'explanation': "There was an error generating a new question. Please review the original explanation: " + explanation[:300]
            }

    except Exception as e:
        # Log unexpected errors in the overall process
        logger.error(f"Unexpected error in generate_test_for_weakness: {str(e)}")

        # Return error indication in a usable format
        return {
            'question': f"TEST_WEAKNESS_{original_question[:50]}... (error occurred)",
            'options': {
                'A': "Error occurred during question generation",
                'B': "Please review the original question",
                'C': "The correct answer to the original question was " + original_correct_text,
                'D': "Error: " + str(e)[:50]
            },
            'correct_answer': 'C',
            'explanation': "An error occurred while generating your test question. Please review the original explanation."
        }

def generate_concept_explanation(question: str, correct_answer: str, user_answer: str) -> str:
    """
    Generate a focused explanation of the core concept being tested in a question.

    Args:
        question: The question text
        correct_answer: The correct answer option
        user_answer: The user's selected answer

    Returns:
        str: Explanation focusing on the core concept and common misconceptions
    """
    # Use mock explanation when API is unavailable
    if not api_key or not client:
        logger.info("Using mock concept explanation due to unavailable OpenAI API")
        return """
        The core concept being tested here is the relationship between neuroanatomical structures and their clinical manifestations.

        This requires understanding:
        1. The precise localization of neural pathways
        2. How specific lesions produce characteristic symptoms
        3. The pattern recognition skills needed for neurological diagnosis

        Common misconceptions include confusing the symptoms of closely related conditions and not recognizing the significance of key discriminating features in the case presentation.
        """

    try:
        logger.info("Generating concept explanation")

        # Create focused prompt for concept explanation
        prompt = f"""# NEUROLOGICAL CONCEPT EXPLANATION REQUEST

## QUESTION CONTEXT
Question: {question}

Correct answer: {correct_answer}

User selected: {user_answer}

## EXPLANATION REQUEST
I need a focused explanation of the core neurological concept being tested in this question. The learner selected an incorrect answer, and I need to help them understand the fundamental principle they're missing.

## REQUIRED COMPONENTS
1. Clear identification of the precise neurological concept/principle being tested
2. Basic science foundation (anatomy, physiology, pathophysiology)
3. Clinical relevance and application of this concept
4. Common misconceptions that lead to selecting incorrect answers
5. Visual or conceptual framework to understand this principle

## FORMAT SPECIFICATIONS
- Keep the explanation concise and focused (approximately 250-300 words)
- Use clear, direct language suitable for a medical student or resident
- Structure with short paragraphs and bullet points for key takeaways
- Bold essential points or terminology
- Emphasize clinical application over theoretical detail
- Focus on the "why" rather than just the "what"

Provide only the concept explanation without any introduction or meta-commentary.
"""

        # Define API parameters
        model = DEFAULT_MODEL
        max_tokens = 1000
        temperature = 0.3  # Lower temperature for focused, accurate explanation

        # Track API call timing
        start_time = time.time()

        try:
            # Primary API call with optimal parameters
            logger.info(f"Calling OpenAI API to generate concept explanation with model: {model}")
            response = chat_completion(
                client,
                model,
                [
                    {"role": "system", "content": "You are a neurology educator specializing in explaining core neurological concepts with clarity and precision. You excel at identifying the fundamental principles underlying complex questions and explaining them in ways that address common misconceptions. Your explanations are concise, evidence-based, and clinically relevant."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=0.9,
                frequency_penalty=0.1,  # Minimal penalty to maintain consistency
                presence_penalty=0.1     # Minimal penalty to maintain focus
            )

            duration = time.time() - start_time
            logger.info(f"Concept explanation generation successful. Duration: {duration:.2f}s")

            # Return the raw explanation content
            out = ''
            try:
                out = get_first_choice_text(response) if response else ''
            except Exception:
                out = ''
            return out or "<p>Unable to generate content at this time.</p>"

        except Exception as error:
            # Log the error and return a fallback explanation
            logger.error(f"Error in generate_concept_explanation: {str(error)}")

            return """
            **Core Concept: Pattern Recognition in Neurological Diagnosis**

            The fundamental principle being tested is the ability to recognize specific patterns of symptoms and signs that point to particular neurological conditions. This requires:

            • Understanding the neural pathways affected in different conditions
            • Recognizing the characteristic constellation of findings in common disorders
            • Distinguishing between conditions with overlapping features

            A common error is focusing on individual symptoms rather than the overall pattern. The key to accurate neurological diagnosis is identifying the specific combination of features that differentiates similar conditions.

            When approaching similar questions, look for the defining characteristics of each condition and match them systematically to the clinical presentation.
            """

    except Exception as e:
        # Log unexpected errors and return a simple explanation
        logger.error(f"Unexpected error in generate_concept_explanation: {str(e)}")

        return f"""
        The core concept being tested in this question relates to neurological pattern recognition and diagnosis.

        When studying this topic further, focus on:
        - The specific symptoms and signs that differentiate similar conditions
        - The underlying pathophysiology that explains these differences
        - The evidence-based approach to diagnosis and management

        Error details: {str(e)[:50]}...
        """

def clinical_reasoning_coach(mcq, selected_answer: str, user_reasoning: str, is_correct: bool = False) -> str:
    """
    Provide step-by-step clinical reasoning guidance based on the user's MCQ answer.
    Adjusts teaching approach based on whether the answer was correct/incorrect and question type.

    Args:
        mcq: The MCQ object containing question and answer data
        selected_answer: The letter of the answer selected by the user
        user_reasoning: The user's explanation for their answer choice
        is_correct: Whether the user's answer was correct

    Returns:
        str: Detailed clinical reasoning guidance with evidence-based feedback
    """
    # Use mock reasoning coach response when API is unavailable
    if not api_key or not client:
        logger.info("Using mock reasoning coach response due to unavailable OpenAI API")
        return get_mock_reasoning_coach(is_correct)
    
    try:
        logger.info(f"Generating clinical reasoning feedback for MCQ with ID: {getattr(mcq, 'id', 'unknown')}")
        logger.info(f"User selected answer: {selected_answer}, Correct: {is_correct}")
        
        # Get options in standardized format
        options_text = format_options_text(mcq)
        question_text = getattr(mcq, 'question_text', 'Question not available')
        correct_answer = getattr(mcq, 'correct_answer', 'A')
        
        # Extract specific option text for both correct answer and selected answer
        correct_option_text = ""
        selected_option_text = ""
        
        try:
            # Get options dictionary using standardized helper
            options_dict = {}
            if hasattr(mcq, 'get_options_dict') and callable(getattr(mcq, 'get_options_dict')):
                options_dict = mcq.get_options_dict()
            elif hasattr(mcq, 'options'):
                if isinstance(mcq.options, str):
                    try:
                        options_dict = json.loads(mcq.options)
                    except json.JSONDecodeError:
                        pass
                elif isinstance(mcq.options, dict):
                    options_dict = mcq.options
                    
            # Extract relevant option text
            for option, text in options_dict.items():
                if option == correct_answer:
                    correct_option_text = text
                if option == selected_answer:
                    selected_option_text = text
        except Exception as opt_error:
            logger.warning(f"Error extracting option text: {str(opt_error)}")
            # Default values if extraction fails
            correct_option_text = f"Option {correct_answer}"
            selected_option_text = f"Option {selected_answer}"
        
        # Determine the question type for specialized guidance
        question_type = determine_question_type(question_text, options_text)
        logger.info(f"Question type identified: {question_type}")
        
        # Get specialized guidance based on question type
        specialized_guidance = {
            "neuroanatomy": """
            ## NEUROANATOMICAL ANALYSIS
            For this neuroanatomy question, include these specific sections:
            
            1. PRECISE LOCALIZATION PATHWAY
               - First determine if the lesion is in CNS vs PNS with specific evidence
               - Provide detailed neuroanatomical localization with specific tracts and nuclei affected
               - Explain the vascular supply or structural relationships of the affected region
               - Create a neuroanatomical diagram showing the relevant structures and relationships
               
            2. FUNCTIONAL CORRELATIONS
               - Connect specific anatomical structures to the presented symptoms/signs
               - Explain why this exact localization produces these specific deficits
               - Differentiate from similar syndromes with closely related localizations
               - Include a table comparing affected pathways and resulting deficits
               
            3. EVIDENCE-BASED APPROACH
               - Cite specific neuroanatomical principles from current literature
               - Explain how modern neuroimaging helps confirm this localization
               - Provide examples of similar cases from clinical practice
               - Include diagnostic criteria for syndromes related to this localization
            """,
            
            "clinical_diagnosis": """
            ## DIAGNOSTIC REASONING FRAMEWORK
            For this clinical diagnosis question, structure your analysis as follows:
            
            1. SYSTEMATIC DIFFERENTIAL DIAGNOSIS
               - Construct a tiered differential based on the presenting symptoms
               - Rank diagnostic possibilities by likelihood based on epidemiology
               - Analyze specific clinical features that support or refute each diagnosis
               - Create a comprehensive comparison table of all diagnostic possibilities
               
            2. EVIDENCE-BASED DIAGNOSTIC APPROACH
               - Present the diagnostic criteria for each condition from 2023-2025 guidelines
               - Specify sensitivity and specificity of key diagnostic findings
               - Outline the appropriate diagnostic workup with proper sequence
               - Explain why certain tests are indicated or contraindicated
               
            3. RED FLAGS & KEY DISCRIMINATORS
               - Identify critical decision points in the diagnostic pathway
               - Highlight "can't miss" diagnoses related to this presentation
               - Explain commonly confused conditions and how to differentiate them
               - Include an algorithm for diagnostic decision-making
            """,
            
            "pharmacotherapy": """
            ## THERAPEUTIC DECISION-MAKING FRAMEWORK
            For this pharmacotherapy question, structure your analysis as follows:
            
            1. EVIDENCE-BASED MEDICATION COMPARISON
               - Create a detailed comparison table with these columns:
                 | Drug | Mechanism | FDA Indications | Efficacy Data | Safety Profile | Drug Interactions | Monitoring |
               - Cite specific studies and guidelines from 2023-2025 for each medication
               - Include evidence levels (A/B/C) for key recommendations
               - Explain first, second, and third-line therapy choices based on guidelines
               
            2. PATIENT-SPECIFIC CONSIDERATIONS
               - Analyze how patient factors affect medication selection
               - Explain age, comorbidity, and genetic considerations
               - Discuss specific contraindications and precautions
               - Address special populations (elderly, pregnancy, renal/hepatic disease)
               
            3. PRACTICAL PRESCRIBING GUIDANCE
               - Provide exact dosing, titration, and administration details
               - Explain monitoring parameters with specific timeframes
               - Describe management of common adverse effects
               - Include criteria for treatment success, failure, and medication switching
            """,
            
            "paraclinical": """
            ## DIAGNOSTIC TESTING INTERPRETATION
            For this paraclinical evidence question, structure your analysis as follows:
            
            1. SYSTEMATIC TEST INTERPRETATION
               - Describe the gold standard findings for each diagnosis
               - Explain sensitivity, specificity, and predictive values of key findings
               - Compare and contrast patterns between similar conditions
               - Create a visual representation of the key diagnostic patterns
               
            2. EVIDENCE-BASED TESTING APPROACH
               - Cite current diagnostic guidelines (2023-2025) with evidence levels
               - Explain the proper sequence of testing based on pretest probability
               - Discuss limitations and potential false positives/negatives
               - Provide cost-effectiveness and availability considerations
               
            3. CLINICAL-PARACLINICAL CORRELATION
               - Connect specific test findings to underlying pathophysiology
               - Explain how test results guide treatment decisions
               - Discuss temporal evolution of findings in disease progression
               - Include monitoring parameters and follow-up testing recommendations
            """,
            
            "general": """
            ## GENERAL CLINICAL REASONING FRAMEWORK
            For this question, structure your analysis as follows:
            
            1. CONCEPTUAL KNOWLEDGE FOUNDATION
               - Identify the core neurological principles being tested
               - Explain key pathophysiological mechanisms involved
               - Connect basic science to clinical manifestations
               - Present a framework for organizing this knowledge area
               
            2. EVIDENCE-BASED APPROACH
               - Cite current guidelines and literature (2023-2025)
               - Provide specific evidence levels for key recommendations
               - Explain evolution of understanding in this area
               - Compare different approaches with their evidence basis
               
            3. CLINICAL DECISION-MAKING
               - Present a step-by-step approach to similar clinical scenarios
               - Identify critical decision points in management
               - Explain common pitfalls and how to avoid them
               - Create a decision-making algorithm for similar cases
            """
        }
        
        # Create differently tailored prompts based on whether the answer was correct
        if is_correct:
            prompt = f"""# CLINICAL REASONING COACH - CORRECT ANSWER ENHANCEMENT

## ROLE DEFINITION
You are ReasoningPal, a renowned neurology educator specializing in advanced clinical reasoning. Your role is to help a learner who answered correctly deepen their understanding of neurological concepts and expand their diagnostic reasoning abilities.

## MCQ INFORMATION
QUESTION: {question_text}

OPTIONS:
{options_text}

CORRECT ANSWER: {correct_answer} - {correct_option_text}

LEARNER'S REASONING: {user_reasoning}

## EXPERT GUIDANCE STRUCTURE
1. STRENGTHS & IMPROVEMENT AREAS
   - Acknowledge the learner's correct conclusion
   - Identify specific strengths in their reasoning process
   - Pinpoint 2-3 areas where their reasoning could be deeper or more evidence-based
   - Note any misconceptions or knowledge gaps revealed in their explanation

2. CLINICAL REASONING FRAMEWORK
   - Provide a structured approach to this type of question using the following framework:
     * Key history elements and their significance
     * Critical physical examination findings
     * Appropriate diagnostic workup
     * Differential diagnosis construction
     * Decision-making process for treatment selection
   - Apply this framework specifically to this MCQ's clinical scenario
   
3. EVIDENCE-BASED FOUNDATION
   
   ### Guidelines & Evidence Levels
   Create a formatted table showing current guidelines:
   
   | Guideline Source | Year | Recommendation | Evidence Level |
   |-----------------|------|----------------|----------------|
   | [Source Name]   | 2024 | [Specific Rec] | A/B/C         |
   
   ### Pathophysiology & Clinical Correlation
   - Explain the underlying mechanisms with clear connections to symptoms
   - Use a structured format:
     * **Mechanism**: [Description]
     * **Clinical Manifestation**: [How it presents]
     * **Diagnostic Correlation**: [How it appears on tests]
   
   ### Recent Advances (2023-2025)
   Format as a list with specific improvements:
   - **Advance 1**: [Description and clinical impact]
   - **Advance 2**: [Description and clinical impact]

4. ADVANCED PATTERN RECOGNITION
   
   ### Key Clinical Patterns Table
   Create a comparison table with proper structure:
   
   | Clinical Feature | Pattern A | Pattern B | Key Discriminator |
   |-----------------|-----------|-----------|-------------------|
   | Onset           | [Detail]  | [Detail]  | [Difference]      |
   | Progression     | [Detail]  | [Detail]  | [Difference]      |
   | Key Finding     | [Detail]  | [Detail]  | [Difference]      |
   
   ### Pattern Recognition Framework
   - **Step 1**: Identify initial presentation category
   - **Step 2**: Look for discriminating features
   - **Step 3**: Apply diagnostic criteria
   - **Step 4**: Confirm with appropriate testing

5. BOARD EXAM STRATEGY
   - Share specific test-taking strategies for this question type
   - Identify common examination pitfalls related to this topic
   - Suggest memory tools for key diagnostic criteria or management steps
   - Connect this concept to other high-yield neurological topics

{specialized_guidance.get(question_type, specialized_guidance["general"])}

## FORMATTING REQUIREMENTS
- Use clean, consistent formatting with proper section headers (###)
- Create properly formatted Markdown tables with these elements:
  * Use pipe characters (|) to separate columns
  * Include header row with separator line (|---|---|)
  * Ensure all cells have content (no empty cells)
  * Keep column widths reasonable for readability
- Structure tables as follows:
  ```
  | Header 1 | Header 2 | Header 3 |
  |----------|----------|----------|
  | Data 1   | Data 2   | Data 3   |
  ```
- Use bullet points for lists of criteria or steps
- Bold key concepts using **text** format
- Create subsections with ### for better organization
- Ensure all table cells contain meaningful content, never leave empty

Maintain a supportive, collegial tone throughout your response. Focus on building conceptual understanding and evidence-based thinking rather than memorization.
"""
        else:
            prompt = f"""# CLINICAL REASONING COACH - ERROR ANALYSIS & CORRECTION

## ROLE DEFINITION
You are ReasoningPal, a premier neurology educator specializing in clinical reasoning development. Your role is to help a learner who answered incorrectly understand their error pattern, correct misconceptions, and build stronger diagnostic reasoning skills.

## MCQ INFORMATION
QUESTION: {question_text}

OPTIONS:
{options_text}

CORRECT ANSWER: {correct_answer} - {correct_option_text}

LEARNER'S INCORRECT CHOICE: {selected_answer} - {selected_option_text}

LEARNER'S REASONING: {user_reasoning}

## EXPERT GUIDANCE STRUCTURE
1. ERROR PATTERN ANALYSIS
   - Identify the specific cognitive error or knowledge gap in the learner's reasoning
   - Explain common misconceptions that lead to this particular error
   - Analyze why option {selected_answer} might seem plausible but is incorrect
   - Highlight the critical thinking step that was missed
   
2. QUESTION DECONSTRUCTION
   - Break down the stem to identify key clinical information
   - Highlight the critical discriminating features
   - Explain what the question is really testing (knowledge domain)
   - Reframe the question to clarify what's being asked

3. EVIDENCE-BASED REASONING PATH
   
   ### Clinical Reasoning Sequence
   - **Step 1: History Analysis**
     * Key elements: [List specific features]
     * Significance: [Explain what they indicate]
   
   - **Step 2: Physical Examination**
     * Critical findings: [List findings]
     * Clinical correlation: [What they mean]
   
   - **Step 3: Diagnostic Approach**
     | Test | Sensitivity | Specificity | When to Use |
     |------|------------|-------------|-------------|
     | [Test 1] | X% | Y% | [Indication] |
     | [Test 2] | X% | Y% | [Indication] |
   
   ### Current Guidelines (2023-2025)
   | Guideline | Recommendation | Evidence Level |
   |-----------|----------------|----------------|
   | [Source]  | [Specific rec] | A/B/C         |

4. COMPARATIVE ANALYSIS TABLE
   
   ### Option Comparison: {selected_answer} vs {correct_answer}
   
   | Feature | Option {selected_answer} | Option {correct_answer} | Key Difference |
   |---------|-------------------------|------------------------|----------------|
   | Clinical Presentation | [Details] | [Details] | [Discriminator] |
   | Pathophysiology | [Details] | [Details] | [Discriminator] |
   | Diagnostic Findings | [Details] | [Details] | [Discriminator] |
   | Treatment Approach | [Details] | [Details] | [Discriminator] |
   | Evidence Support | [Details] | [Details] | [Discriminator] |
   
   ### Critical Discriminating Features
   - **Feature 1**: [Explanation of why this distinguishes the answers]
   - **Feature 2**: [Explanation of why this distinguishes the answers]

5. KNOWLEDGE CONSOLIDATION
   - Provide a framework for remembering this concept
   - Connect this topic to broader neurological principles
   - Share high-yield board exam tips related to this topic
   - Suggest focused study resources for this knowledge gap

{specialized_guidance.get(question_type, specialized_guidance["general"])}

## FORMATTING REQUIREMENTS
- Use clean, consistent formatting with proper section headers (###)
- Create properly formatted Markdown tables with these elements:
  * Use pipe characters (|) to separate columns
  * Include header row with separator line (|---|---|)
  * Ensure all cells have content (no empty cells)
  * Keep column widths reasonable for readability
- Structure tables as follows:
  ```
  | Header 1 | Header 2 | Header 3 |
  |----------|----------|----------|
  | Data 1   | Data 2   | Data 3   |
  ```
- Use bullet points for lists and diagnostic criteria
- Bold key concepts using **text** format
- Create subsections with ### for better organization
- Ensure all table cells contain meaningful content, never leave empty
- Use proper spacing between sections for readability

Maintain a supportive, non-judgmental tone throughout your response. Focus on building stronger clinical reasoning rather than simply correcting the error.
"""
        
        # Define API parameters
        model = DEFAULT_MODEL
        max_tokens = 5000
        temperature = 0.4
        
        # Track API call timing
        start_time = time.time()
        
        # Content generation with error handling and retry logic
        try:
            # Primary API call with optimal parameters
            logger.info(f"Calling OpenAI API for clinical reasoning coaching with model: {model}")
            response = chat_completion(
                client,
                model,
                [
                    {"role": "system", "content": "You are ReasoningPal, an expert neurologist and board-certified educator specializing in evidence-based clinical reasoning. Your expertise includes neuroanatomy, clinical diagnosis, pharmacotherapy, and diagnostic interpretation. You prioritize the most current guidelines (2023-2025) and always cite evidence levels. You excel at creating structured, properly formatted Markdown tables with clear headers, separator lines, and complete content in every cell. You maintain a supportive, educational tone while providing precise, factually accurate information. When presenting treatment recommendations, you always include level of evidence classifications (A/B/C). Format all content with clear hierarchical structure using ### for subsection headers, proper spacing between sections, and consistent Markdown table formatting with | for columns and |---| for header separators. Never leave table cells empty - always provide meaningful content."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=0.9,
                presence_penalty=0.2,  # Encourage exploration of reasoning pathways
                frequency_penalty=0.2   # Prevent repetitive explanations
            )
            
            duration = time.time() - start_time
            logger.info(f"Clinical reasoning coaching successful. Duration: {duration:.2f}s")
            
            out2 = ''
            try:
                out2 = get_first_choice_text(response) if response else ''
            except Exception:
                out2 = ''
            return out2 or "{}"
            
        except Exception as primary_error:
            # Log the primary error details
            error_type = type(primary_error).__name__
            error_msg = str(primary_error)
            logger.warning(f"Primary API error in clinical_reasoning_coach ({error_type}): {error_msg}")
            
            # Retry with more conservative parameters
            try:
                logger.info("Retrying clinical reasoning coaching with conservative parameters")
                response = chat_completion(
                    client,
                    model,
                    [
                        {"role": "system", "content": "You are ReasoningPal, an expert neurologist who specializes in teaching clinical reasoning skills to medical students."},
                        {"role": "user", "content": prompt}
                    ],
                    max_tokens=2000,  # Reduced token count
                    temperature=0.5    # Slightly higher temperature
                )
                
                retry_duration = time.time() - start_time
                logger.info(f"Clinical reasoning coaching retry successful. Total duration: {retry_duration:.2f}s")
                
                return get_first_choice_text(response)
                
            except Exception as retry_error:
                # If retry also fails, return a mock response
                retry_error_type = type(retry_error).__name__
                retry_error_msg = str(retry_error)
                logger.error(f"Clinical reasoning coaching retry failed ({retry_error_type}): {retry_error_msg}")
                
                # Return mock coaching response based on whether answer was correct
                logger.info(f"Falling back to mock reasoning coach response (is_correct={is_correct})")
                return get_mock_reasoning_coach(is_correct)
    
    except Exception as e:
        # Log unexpected errors in the overall process
        logger.error(f"Unexpected error in clinical_reasoning_coach: {str(e)}")
        return f"""# ERROR IN CLINICAL REASONING COACH

Unfortunately, we encountered an error while processing your request. 

## Technical Details
Error: {str(e)}

## Alternative Feedback
Here is some general feedback on clinical reasoning for this question:

{get_mock_reasoning_coach(is_correct)}
"""


# AI-Powered MCQ Editing Functions

def ai_edit_question(mcq, custom_instructions: str = "") -> str:
    """
    AI-powered question text editing that maintains the original concept while improving clarity.
    Uses the OpenAI Responses API with JSON schema enforcement and vector store support.
    
    Args:
        mcq: The MCQ object containing the question to edit
        custom_instructions: Optional custom instructions from the user
        
    Returns:
        str: The improved question text
    """
    if not api_key or not client:
        logger.info("Using mock question edit due to unavailable OpenAI API")
        return getattr(mcq, "question_text", "")

    mcq_id = getattr(mcq, "id", "unknown")
    question_text = getattr(mcq, "question_text", "") or ""
    if not question_text.strip():
        return "No question text provided."

    # Resolve existing options for prompt context
    options_dict: Dict[str, Any] = {}
    if hasattr(mcq, "get_options_dict") and callable(getattr(mcq, "get_options_dict")):
        try:
            options_dict = mcq.get_options_dict() or {}
        except Exception:
            options_dict = {}
    elif hasattr(mcq, "options"):
        if isinstance(mcq.options, dict):
            options_dict = mcq.options
        elif isinstance(mcq.options, str):
            try:
                options_dict = json.loads(mcq.options)
            except json.JSONDecodeError:
                options_dict = {}

    options_text = format_options_text(mcq) or "No answer choices available."
    correct_answer = getattr(mcq, "correct_answer", "").strip()
    instructions_lower = (custom_instructions or "").lower()
    forbidden_terms = _extract_forbidden_terms(custom_instructions)
    wants_options = bool(
        re.search(r"\b(multiple[-\s]?choice|answer choices?|options?)\b", instructions_lower)
    )

    schema: Dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "stem": {"type": "string", "minLength": 120},
        },
        "required": ["stem"],
    }
    if wants_options:
        schema["properties"]["options"] = {
            "type": "object",
            "additionalProperties": False,
            "required": ["A", "B", "C", "D"],
            "properties": {
                letter: {"type": "string", "minLength": 12}
                for letter in ("A", "B", "C", "D")
            },
        }
        if "options" not in schema["required"]:
            schema["required"].append("options")

    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "mcq_question_revision",
            "schema": schema,
        },
    }

    if forbidden_terms:
        forbidden_bullets = "\n".join(f"- {term}" for term in forbidden_terms)
        prohibited_block = f"Forbidden phrases that must NOT appear anywhere: \n{forbidden_bullets}"
    else:
        prohibited_block = ""

    if custom_instructions:
        custom_block = f"Custom instructions from the editor:\n{custom_instructions}"
    else:
        custom_block = ""

    quality_requirements = [
        "Preserve the exact clinical intent, key findings, and difficulty level tested by the original question.",
        "Write a polished neurology board-style vignette with rich clinical detail.",
        "Follow a logical flow: demographics → chief concern → timeline/course → pertinent examination and investigations.",
        f"Meet or exceed {QUESTION_MIN_WORDS} words AND {QUESTION_MIN_CHARS} characters.",
        "Do not mention the editing process or that this is a rewritten stem.",
        "Do not reveal or hint at the correct answer.",
        "Ensure the final question reads like a concise USMLE/ABPN-style prompt with crisp grammar and professional tone.",
        "Keep the stem concise (≤ 180 words) and include only clinical details that are necessary for answering the question.",
        "Improve clarity and organization without inventing new clinical data; only rephrase or polish what is already implied by the original stem.",
        "Avoid internal contradictions and keep all demographic details, exam findings, and timelines self-consistent with the source question.",
        "Preserve the original difficulty by avoiding early or repetitive emphasis of hallmark clues—embed critical findings among neutral details so the examinee must synthesize the scenario.",
        "Balance the vignette with at least one plausible but non-diagnostic detail to prevent the answer from feeling obvious while still supporting the intended diagnosis.",
    ]
    if wants_options:
        quality_requirements.append(
            "Return four mutually exclusive answer choices labelled A) through D) that remain compatible with the same correct diagnosis, and ensure the stem clearly leads to a single best option."
        )
    if forbidden_terms:
        quality_requirements.append("Avoid every forbidden phrase exactly as listed.")

    quality_block = "\n".join(f"- {item}" for item in quality_requirements)

    user_prompt_parts = [
        "# ORIGINAL QUESTION STEM",
        question_text,
        "\n## EXISTING ANSWER OPTIONS",
        options_text or "Not provided.",
        f"\n## CORRECT ANSWER (REFERENCE ONLY): {correct_answer or 'Unknown'}",
        "\n## REVISION REQUIREMENTS",
        quality_block,
    ]
    if custom_block:
        user_prompt_parts.append("\n## USER PREFERENCES\n" + custom_block)
    if prohibited_block:
        user_prompt_parts.append("\n## FORBIDDEN TERMS\n" + prohibited_block)

    user_prompt = "\n".join(user_prompt_parts)

    system_prompt = (
        "You are a board-certified neurologist and medical educator. "
        "Rewrite MCQ vignettes to improve clarity, structure, and educational value while preserving the diagnosis. "
        "Follow the JSON schema provided by the developer exactly. "
        "Consult any attached knowledge base entries to enrich the scenario, but never contradict the original answer."
    )

    base_messages = [
        {"role": "system", "content": [{"type": "text", "text": system_prompt}]},
        {"role": "user", "content": [{"type": "text", "text": user_prompt}]},
    ]

    feedback_template = (
        "The previous draft was rejected because:\n{issues}\n"
        "Produce a new revision that resolves every item. Do not reuse earlier phrasing."
    )

    max_attempts = 3
    last_errors: List[str] = []

    for attempt in range(max_attempts):
        messages = list(base_messages)
        if attempt and last_errors:
            issues_text = "\n".join(f"- {err}" for err in last_errors)
            messages.append(
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "text",
                            "text": feedback_template.format(issues=issues_text),
                        }
                    ],
                }
            )
        try:
            response = _responses_create(
                DEFAULT_MODEL,
                messages,
                response_format=response_format,
                max_output_tokens=2000,
                temperature=0.45 if wants_options else 0.35,
                top_p=0.9,
                use_vector=True,
            )
        except Exception as api_error:
            should_retry, message, status = _classify_openai_error(api_error)
            logger.warning(
                "OpenAI question edit failed for MCQ #%s (attempt %s/%s): %s",
                mcq_id,
                attempt + 1,
                max_attempts,
                message,
            )
            if should_retry and attempt + 1 < max_attempts:
                time.sleep(min(2 ** attempt, 3))
                last_errors = [message]
                continue
            raise ValueError(message) from api_error

        payload, raw_text = _extract_response_json(response)
        if not payload:
            snippet = (raw_text or "").strip()
            if len(snippet) > 160:
                snippet = snippet[:160] + "..."
            last_errors = [f"Model did not return valid JSON. Raw output: {snippet or '[empty]'}"]
            continue

        stem = str(payload.get("stem", "")).strip()
        if not stem:
            last_errors = ["JSON schema output missing 'stem' content."]
            continue

        options_output: Dict[str, str] = {}
        if wants_options:
            raw_options = payload.get("options", {})
            if not isinstance(raw_options, dict):
                last_errors = ["JSON schema output missing object 'options' with keys A-D."]
                continue
            options_output = {
                letter: str(raw_options.get(letter, "")).strip()
                for letter in ("A", "B", "C", "D")
            }
            missing_letters = [letter for letter, text in options_output.items() if not text]
            if missing_letters:
                last_errors = [f"Missing text for options: {', '.join(missing_letters)}"]
                continue

        final_text = stem
        if wants_options:
            option_lines = [f"{letter}) {options_output[letter]}" for letter in ("A", "B", "C", "D")]
            final_text = stem.rstrip() + "\n\n" + "\n".join(option_lines)

        validation_errors = _validate_question_revision(
            original=question_text,
            revised=final_text,
            wants_options=wants_options,
            forbidden_terms=forbidden_terms,
        )
        if not validation_errors:
            logger.info(
                "Successfully improved question for MCQ #%s on attempt %s",
                mcq_id,
                attempt + 1,
            )
            return final_text

        last_errors = validation_errors
        logger.warning(
            "Question revision failed validation for MCQ #%s (attempt %s/%s): %s",
            mcq_id,
            attempt + 1,
            max_attempts,
            "; ".join(validation_errors),
        )

    error_message = "; ".join(last_errors) if last_errors else "unknown error"
    raise ValueError(f"AI question edit failed validation: {error_message}")


def ai_edit_options(mcq, custom_instructions: str = "") -> dict:
    """
    AI-powered option generation that ONLY fills missing options with USMLE-style distractors.
    Existing options are preserved unchanged.
    
    Args:
        mcq: The MCQ object containing current options
        custom_instructions: Optional custom instructions from the user
        
    Returns:
        dict: Dictionary with all options, filling only the missing ones
    """
    if not api_key or not client:
        logger.info("Using original options due to unavailable OpenAI API")
        return mcq.get_options_dict() if hasattr(mcq, 'get_options_dict') else {}
    
    try:
        mcq_id = getattr(mcq, "id", "unknown")
        logger.info("AI filling missing options for MCQ #%s", mcq_id)

        question_text = getattr(mcq, "question_text", "")
        current_options = mcq.get_options_dict() if hasattr(mcq, "get_options_dict") else {}
        correct_answer = getattr(mcq, "correct_answer", "A")

        missing_options: List[str] = []
        existing_options: Dict[str, str] = {}
        for opt in ["A", "B", "C", "D"]:
            option_text = str(current_options.get(opt, "")).strip()
            if option_text:
                existing_options[opt] = option_text
            else:
                missing_options.append(opt)

        if not missing_options:
            logger.info("No missing options detected; returning original options.")
            return current_options

        explanation = getattr(mcq, "explanation", "")
        subspecialty = getattr(mcq, "subspecialty", "General Neurology")
        forbidden_terms = _extract_forbidden_terms(custom_instructions)

        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                opt: {"type": "string", "minLength": 12}
                for opt in missing_options
            },
            "required": missing_options,
        }
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "mcq_missing_options",
                "schema": schema,
            },
        }

        if forbidden_terms:
            forbidden_text = "\n".join(f"- {term}" for term in forbidden_terms)
            prohibited_block = (
                "Avoid the following phrases entirely:\n" + forbidden_text
            )
        else:
            prohibited_block = ""

        if custom_instructions:
            custom_block = "Custom instructions from editor:\n" + custom_instructions
        else:
            custom_block = ""

        existing_lines = "\n".join(f"{opt}) {text}" for opt, text in existing_options.items())

        user_prompt_sections = [
            "# QUESTION STEM",
            question_text,
            "\n# EXISTING OPTIONS (LOCKED)",
            existing_lines or "No existing distractors.",
            "\n# OPTIONS TO GENERATE",
            ", ".join(missing_options),
            f"\n# CORRECT ANSWER (REFERENCE ONLY): {correct_answer}",
            f"\n# SUBSPECIALTY: {subspecialty}",
            "\n# REQUIREMENTS",
            "- Generate only the missing options listed above.",
            "- Each distractor must be clinically plausible but ultimately incorrect.",
            "- Match the tone, length, and complexity of the existing options.",
            "- Target common neurology board exam misconceptions.",
            "- Avoid revealing or contradicting the correct answer.",
        ]
        if explanation:
            user_prompt_sections.append(
                "\n# CONTEXT FROM EXPLANATION (TRUNCATED)\n" + str(explanation)[:600]
            )
        if custom_block:
            user_prompt_sections.append("\n# USER CUSTOM INSTRUCTIONS\n" + custom_block)
        if prohibited_block:
            user_prompt_sections.append("\n# FORBIDDEN TERMS\n" + prohibited_block)

        user_prompt = "\n".join(user_prompt_sections)

        system_prompt = (
            "You are a USMLE neurology item writer. "
            "Generate only the missing distractor options specified by the user while leaving existing options untouched. "
            "Follow the JSON schema provided and use attached references when available."
        )

        base_messages = [
            {"role": "system", "content": [{"type": "text", "text": system_prompt}]},
            {"role": "user", "content": [{"type": "text", "text": user_prompt}]},
        ]

        feedback_template = (
            "Previous attempt was rejected because:\n{issues}\n"
            "Return ONLY the requested option keys with improved distractors."
        )

        max_attempts = 3
        last_errors: List[str] = []

        for attempt in range(max_attempts):
            messages = list(base_messages)
            if attempt and last_errors:
                issues_text = "\n".join(f"- {err}" for err in last_errors)
                messages.append(
                    {
                        "role": "system",
                        "content": [
                            {
                                "type": "text",
                                "text": feedback_template.format(issues=issues_text),
                            }
                        ],
                    }
                )

            try:
                response = _responses_create(
                    DEFAULT_MODEL,
                    messages,
                    response_format=response_format,
                    max_output_tokens=400,
                    temperature=0.55,
                    top_p=0.9,
                    use_vector=True,
                )
            except Exception as api_error:
                should_retry, message, status = _classify_openai_error(api_error)
                logger.warning(
                    "OpenAI missing-option fill failed for MCQ #%s (attempt %s/%s): %s",
                    mcq_id,
                    attempt + 1,
                    max_attempts,
                    message,
                )
                if should_retry and attempt + 1 < max_attempts:
                    time.sleep(min(2 ** attempt, 3))
                    last_errors = [message]
                    continue
                raise ValueError(message) from api_error

            payload, raw_text = _extract_response_json(response)
            if not payload:
                snippet = (raw_text or "").strip()
                if len(snippet) > 160:
                    snippet = snippet[:160] + "..."
                last_errors = [f"Invalid JSON output: {snippet or '[empty]'}"]
                continue

            missing_remaining = [
                opt
                for opt in missing_options
                if not str(payload.get(opt, "")).strip()
            ]
            if missing_remaining:
                last_errors = [
                    f"Missing distractors for {', '.join(missing_remaining)}."
                ]
                continue

            if forbidden_terms:
                hits: List[str] = []
                for text in payload.values():
                    hits.extend(_detect_forbidden_hits(str(text), forbidden_terms))
                if hits:
                    unique_hits = ", ".join(sorted(set(hits)))
                    last_errors = [f"Response contained forbidden terms: {unique_hits}"]
                    continue

            final_options = current_options.copy()
            for opt in missing_options:
                final_options[opt] = str(payload.get(opt, "")).strip()

            logger.info(
                "Successfully filled missing options for MCQ #%s on attempt %s",
                mcq_id,
                attempt + 1,
            )
            return final_options

        error_message = "; ".join(last_errors) if last_errors else "unknown error"
        raise ValueError(error_message)

    except Exception as e:
        should_retry, message, status_code = _classify_openai_error(e)
        logger.error("Error in ai_edit_options for MCQ #%s: %s", getattr(mcq, "id", "unknown"), message)
        raise ValueError(message) from e


def ai_improve_all_options(mcq, custom_instructions: str = "") -> dict:
    """
    AI-powered option improvement that enhances ALL options (except correct answer) to be
    educationally valuable USMLE-style distractors.
    
    Args:
        mcq: The MCQ object containing current options
        custom_instructions: Optional custom instructions from the user
        
    Returns:
        dict: Dictionary with all options, with incorrect ones improved
    """
    if not api_key or not client:
        logger.info("Using original options due to unavailable OpenAI API")
        return mcq.get_options_dict() if hasattr(mcq, 'get_options_dict') else {}
    
    try:
        mcq_id = getattr(mcq, "id", "unknown")
        logger.info("AI improving all options for MCQ #%s", mcq_id)

        question_text = getattr(mcq, "question_text", "")
        current_options = mcq.get_options_dict() if hasattr(mcq, "get_options_dict") else {}
        correct_answer = getattr(mcq, "correct_answer", "A")
        original_correct_text = str(current_options.get(correct_answer, "")).strip()

        explanation = getattr(mcq, "explanation", "")
        subspecialty = getattr(mcq, "subspecialty", "General Neurology")
        forbidden_terms = _extract_forbidden_terms(custom_instructions)

        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["A", "B", "C", "D"],
            "properties": {
                letter: {"type": "string", "minLength": 12}
                for letter in ("A", "B", "C", "D")
            },
        }

        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "mcq_option_improvement",
                "schema": schema,
            },
        }

        if forbidden_terms:
            forbidden_text = "\n".join(f"- {term}" for term in forbidden_terms)
            prohibited_block = "Never include the following phrases:\n" + forbidden_text
        else:
            prohibited_block = ""

        if custom_instructions:
            custom_block = "Custom instructions from editor:\n" + custom_instructions
        else:
            custom_block = ""

        current_lines = "\n".join(
            f"{opt}) {current_options.get(opt, '')}" for opt in ("A", "B", "C", "D")
        )

        user_prompt_sections = [
            "# QUESTION STEM",
            question_text,
            "\n# CURRENT OPTIONS",
            current_lines or "Options not provided.",
            f"\n# CORRECT ANSWER IDENTIFIER: {correct_answer}",
            "\n# TASK",
            "- Keep the correct answer text exactly the same (aside from correcting obvious typos).",
            "- Upgrade every incorrect option into a high-quality USMLE-style distractor that is plausible but ultimately wrong.",
            "- Ensure each distractor highlights a distinct misconception or differential diagnosis.",
            "- Maintain comparable length, tone, and specificity across options.",
            "- Avoid reiterating the same idea in multiple choices.",
        ]
        if explanation:
            user_prompt_sections.append(
                "\n# EXPLANATION CONTEXT (TRUNCATED)\n" + str(explanation)[:600]
            )
        user_prompt_sections.append(f"\n# SUBSPECIALTY: {subspecialty}")
        if custom_block:
            user_prompt_sections.append("\n# USER CUSTOM INSTRUCTIONS\n" + custom_block)
        if prohibited_block:
            user_prompt_sections.append("\n# FORBIDDEN TERMS\n" + prohibited_block)

        user_prompt = "\n".join(user_prompt_sections)

        system_prompt = (
            "You are a neurology board-exam content specialist. "
            "Refine MCQ answer choices so incorrect distractors are educational and clinically grounded while the correct answer remains untouched. "
            "Follow the JSON schema supplied by the developer."
        )

        base_messages = [
            {"role": "system", "content": [{"type": "text", "text": system_prompt}]},
            {"role": "user", "content": [{"type": "text", "text": user_prompt}]},
        ]

        feedback_template = (
            "Previous attempt failed because:\n{issues}\n"
            "Return options A-D as strings, keeping the correct answer text unchanged."
        )

        max_attempts = 3
        last_errors: List[str] = []

        for attempt in range(max_attempts):
            messages = list(base_messages)
            if attempt and last_errors:
                issues_text = "\n".join(f"- {err}" for err in last_errors)
                messages.append(
                    {
                        "role": "system",
                        "content": [
                            {
                                "type": "text",
                                "text": feedback_template.format(issues=issues_text),
                            }
                        ],
                    }
                )

            try:
                response = _responses_create(
                    DEFAULT_MODEL,
                    messages,
                    response_format=response_format,
                    max_output_tokens=600,
                    temperature=0.6,
                    top_p=0.9,
                    use_vector=True,
                )
            except Exception as api_error:
                should_retry, message, status = _classify_openai_error(api_error)
                logger.warning(
                    "OpenAI improve-all options failed for MCQ #%s (attempt %s/%s): %s",
                    mcq_id,
                    attempt + 1,
                    max_attempts,
                    message,
                )
                if should_retry and attempt + 1 < max_attempts:
                    time.sleep(min(2 ** attempt, 3))
                    last_errors = [message]
                    continue
                raise ValueError(message) from api_error

            payload, raw_text = _extract_response_json(response)
            if not payload:
                snippet = (raw_text or "").strip()
                if len(snippet) > 160:
                    snippet = snippet[:160] + "..."
                last_errors = [f"Invalid JSON output: {snippet or '[empty]'}"]
                continue

            missing_letters = [
                letter for letter in ("A", "B", "C", "D")
                if not str(payload.get(letter, "")).strip()
            ]
            if missing_letters:
                last_errors = [f"Missing updated text for: {', '.join(missing_letters)}"]
                continue

            if forbidden_terms:
                hits: List[str] = []
                for text in payload.values():
                    hits.extend(_detect_forbidden_hits(str(text), forbidden_terms))
                if hits:
                    unique_hits = ", ".join(sorted(set(hits)))
                    last_errors = [f"Response contained forbidden terms: {unique_hits}"]
                    continue

            updated_options = {
                letter: str(payload.get(letter, "")).strip()
                for letter in ("A", "B", "C", "D")
            }

            if correct_answer in updated_options and original_correct_text:
                if (
                    updated_options[correct_answer]
                    and updated_options[correct_answer] != original_correct_text
                ):
                    updated_options[correct_answer] = original_correct_text

            logger.info(
                "Successfully improved all options for MCQ #%s on attempt %s",
                mcq_id,
                attempt + 1,
            )
            return updated_options

        error_message = "; ".join(last_errors) if last_errors else "unknown error"
        raise ValueError(error_message)

    except Exception as e:
        should_retry, message, status_code = _classify_openai_error(e)
        logger.error("Error in ai_improve_all_options for MCQ #%s: %s", getattr(mcq, "id", "unknown"), message)
        raise ValueError(message) from e


def ai_edit_explanation_text(
    mcq,
    current_content: str = "",
    custom_instructions: str = "",
    *,
    mode: str = "enhance",
) -> str:
    """Generate or enhance the unified explanation for an MCQ."""
    python_client_available = bool(api_key and client)
    if not python_client_available and not USE_AGENT_FOR_EXPLANATIONS:
        logger.info("Using original explanation due to unavailable OpenAI API")
        return current_content

    mode = mode if mode in {"enhance", "rewrite"} else "enhance"

    try:
        mcq_id = getattr(mcq, "id", "unknown")
        logger.info(
            "AI editing unified explanation for MCQ #%s (mode=%s)",
            mcq_id,
            mode,
        )

        question_text = getattr(mcq, "question_text", "")
        options = format_options_text(mcq)
        correct_answer = getattr(mcq, "correct_answer", "")
        subspecialty = getattr(mcq, "subspecialty", "General Neurology")
        forbidden_terms = _extract_forbidden_terms(custom_instructions)

        reference_text = (current_content or "").strip()
        if not reference_text and mode == "enhance":
            reference_text = (
                getattr(mcq, "unified_explanation", "")
                or getattr(mcq, "explanation", "")
                or ""
            ).strip()

        baseline_chars = len(reference_text)
        min_chars = max(600, int(baseline_chars * 0.85) if baseline_chars else 750)
        min_words = max(120, min_chars // 5)

        def _validate_explanation_candidate(candidate: str) -> List[str]:
            errors_local: List[str] = []
            text = (candidate or "").strip()

            if not text:
                errors_local.append("Model returned empty explanation text.")
                return errors_local

            if len(text) < min_chars:
                errors_local.append(
                    f"Explanation too short ({len(text)} chars); need ≥ {min_chars}."
                )

            word_count_local = _question_word_count(text)
            if word_count_local < min_words:
                errors_local.append(
                    f"Explanation too brief ({word_count_local} words); need ≥ {min_words}."
                )

            if forbidden_terms:
                hits = _detect_forbidden_hits(text, forbidden_terms)
                if hits:
                    errors_local.append(
                        "Content contains prohibited terms: " + ", ".join(sorted(set(hits)))
                    )

            return errors_local

        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "explanation": {
                    "type": "string",
                    "minLength": max(320, min_chars // 2),
                }
            },
            "required": ["explanation"],
        }
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "mcq_unified_explanation",
                "schema": schema,
            },
        }

        prompt_sections_base = [
            f"Neurology subspecialty: {subspecialty or 'General Neurology'}",
            "Question stem:\n" + question_text.strip(),
            "Options (with correct answer marked *):\n" + options.strip(),
            f"Correct answer letter: {correct_answer or 'Unknown'}",
        ]

        if reference_text and mode == "enhance":
            prompt_sections_base.append(
                "Current explanation draft (improve clarity, evidence, and organisation without losing key facts):\n"
                + reference_text
            )
        elif reference_text:
            prompt_sections_base.append(
                "Previous explanation draft (for reference only; generate a fresh explanation with new wording):\n"
                + reference_text
            )
        else:
            prompt_sections_base.append("No existing explanation is available. Craft a full explanation from scratch.")

        guidance_lines_base = [
            "Write a single unified explanation suitable for a physician preparing for board exams.",
            "Use short markdown headings (### Heading) to break up major ideas.",
            "Explain why the correct answer is right, why each distractor falls short, and include clinical pearls.",
            "Incorporate high-yield pathophysiology, distinguishing clinical features, and management insights backed by 2023-2025 guidance.",
            "Keep the tone professional, concise, and evidence-based.",
        ]

        if forbidden_terms:
            guidance_lines_base.append(
                "Do not use the following phrases under any circumstance:\n"
                + "\n".join(f"- {term}" for term in forbidden_terms)
            )

        if custom_instructions:
            guidance_lines_base.append(
                "Author-specific preferences:\n" + custom_instructions.strip()
            )

        guidance_lines_base.append(
            "Respond with valid JSON only: {\"explanation\": \"...\"}."
        )

        sanitized_question_text = _sanitize_for_policy(question_text)
        sanitized_options_text = _sanitize_for_policy(options)
        prompt_sanitized = False

        user_prompt = "\n\n".join(prompt_sections_base + guidance_lines_base)

        agent_explanation = _run_agent_explanation(
            user_prompt,
            workflow_id=f"wf_explanation_{mcq_id}",
            context={
                "mcq_id": mcq_id,
                "mode": mode,
            },
        )
        if agent_explanation:
            agent_errors = _validate_explanation_candidate(agent_explanation)
            if not agent_errors:
                logger.info(
                    "Using agent SDK explanation for MCQ #%s", mcq_id
                )
                return agent_explanation
            logger.warning(
                "Agent explanation failed validation for MCQ #%s: %s",
                mcq_id,
                "; ".join(agent_errors),
            )

        if not python_client_available:
            logger.info(
                "Python OpenAI client unavailable; returning existing explanation content."
            )
            return reference_text or current_content

        system_prompt = (
            "You are a board-certified neurologist and medical educator. "
            "Produce evidence-based, guideline-aligned MCQ explanations with impeccable organisation. "
            "Always return valid JSON conforming to the requested schema."
        )

        if custom_instructions:
            system_prompt += (
                "\nRespect the following editor directives while drafting the explanation: "
                + custom_instructions.strip()
            )

        feedback_template = (
            "The previous attempt was rejected because:\n{issues}\n"
            "Produce a revised draft resolving every issue while maintaining alignment with the correct answer."
        )

        max_attempts = 3
        last_errors = []

        for attempt in range(max_attempts):
            current_sections = list(prompt_sections_base)
            current_guidance = list(guidance_lines_base)

            if prompt_sanitized:
                sanitized_question = sanitized_question_text or (
                    "Clinical details redacted for policy compliance. Base your explanation on the general presentation and core neurologic principles."
                    if question_text
                    else "Clinical details redacted."
                )
                sanitized_options = sanitized_options_text or (
                    "Highlight why the correct answer is preferred and summarize key pitfalls for the other answer choices without quoting them."
                )
                if len(current_sections) >= 2:
                    current_sections[1] = "Question stem (sanitized for policy compliance):\n" + sanitized_question
                if len(current_sections) >= 3:
                    current_sections[2] = (
                        "Options summary (sanitized):\n" + sanitized_options
                    )
                current_guidance.append(
                    "The original question content has been sanitized due to safety filters. Rely on accepted neurologic knowledge, avoid conjecture, and do not mention that sanitization occurred."
                )

            user_prompt_current = "\n\n".join(current_sections + current_guidance)

            base_messages = [
                {"role": "system", "content": [{"type": "text", "text": system_prompt}]},
                {"role": "user", "content": [{"type": "text", "text": user_prompt_current}]},
            ]

            messages = list(base_messages)
            if attempt and last_errors:
                issues_text = "\n".join(f"- {err}" for err in last_errors)
                messages.append(
                    {
                        "role": "system",
                        "content": [
                            {
                                "type": "text",
                                "text": feedback_template.format(issues=issues_text),
                            }
                        ],
                    }
                )

            try:
                response = _responses_create(
                    DEFAULT_MODEL,
                    messages,
                    response_format=response_format,
                    max_output_tokens=1400,
                    temperature=0.35,
                    top_p=0.9,
                    use_vector=True,
                )
            except Exception as api_error:
                should_retry, message, status = _classify_openai_error(api_error)
                logger.warning(
                    "Unified explanation edit failed for MCQ #%s (attempt %s/%s): %s",
                    mcq_id,
                    attempt + 1,
                    max_attempts,
                    message,
                )
                if "invalid prompt" in message.lower() and not prompt_sanitized:
                    prompt_sanitized = True
                    last_errors = [
                        "Prompt was flagged by safety filters; retrying with sanitized context."
                    ]
                    continue
                if should_retry and attempt + 1 < max_attempts:
                    time.sleep(min(2 ** attempt, 3))
                    last_errors = [message]
                    continue
                raise ValueError(message) from api_error

            payload, raw_text = _extract_response_json(response)
            if not payload:
                snippet = (raw_text or "").strip()
                if len(snippet) > 160:
                    snippet = snippet[:160] + "..."
                last_errors = [f"Invalid JSON output: {snippet or '[empty]'}"]
                continue

            explanation = str(payload.get("explanation", "")).strip()
            validation_errors = _validate_explanation_candidate(explanation)
            if validation_errors:
                last_errors = validation_errors
                continue

            logger.info(
                "Successfully generated unified explanation for MCQ #%s on attempt %s",
                mcq_id,
                attempt + 1,
            )
            return explanation

        error_message = "; ".join(last_errors) if last_errors else "unknown error"
        raise ValueError(error_message)

    except Exception as e:
        should_retry, message, status_code = _classify_openai_error(e)
        logger.error(
            "Error editing unified explanation for MCQ #%s: %s",
            getattr(mcq, "id", "unknown"),
            message,
        )
        raise ValueError(message) from e


def regenerate_unified_explanation(mcq, custom_instructions: str = "") -> str:
    """Rewrite the entire MCQ explanation using the AI helper."""
    baseline = (
        getattr(mcq, "unified_explanation", "")
        or getattr(mcq, "explanation", "")
        or ""
    )
    return ai_edit_explanation_text(
        mcq,
        current_content=baseline,
        custom_instructions=custom_instructions,
        mode="rewrite",
    )


def chat_completion(api_client, model, messages, **kwargs):
    # Normalize token parameter name for GPT‑5 models
    if 'max_tokens' in kwargs:
        mt = kwargs.pop('max_tokens')
        if str(model).startswith('gpt-5'):
            kwargs['max_completion_tokens'] = mt
        else:
            kwargs['max_tokens'] = mt
    # GPT-5 models currently only accept default sampling parameters; drop overrides.
    if str(model).startswith('gpt-5'):
        for noisy_param in ('temperature', 'top_p', 'frequency_penalty', 'presence_penalty'):
            kwargs.pop(noisy_param, None)
    return api_client.chat.completions.create(
        model=model,
        messages=messages,
        **kwargs,
    )
