"""
Celery tasks for background processing
"""

import logging
from celery import shared_task
from django.utils import timezone
from django.core.cache import cache
import json

from .openai_integration import (
    get_first_choice_text,
    ai_edit_question,
    ai_edit_options,
    ai_improve_all_options,
    ai_edit_explanation_text,
    regenerate_unified_explanation,
)
from .models import AIEditJob, MCQ

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3)
def process_clinical_reasoning_analysis(self, session_id, mcq_id, selected_answer, user_reasoning, is_correct):
    """
    Background task to process clinical reasoning analysis using OpenAI
    
    Args:
        session_id: CognitiveReasoningSession ID
        mcq_id: MCQ ID
        selected_answer: User's selected answer
        user_reasoning: User's reasoning text
        is_correct: Whether the answer was correct
    """
    try:
        from .models import CognitiveReasoningSession, MCQ
        # Prefer OpenAI-backed generator; gracefully fall back to rule-based if unavailable
        try:
            from .cognitive_analysis_openai import ReasoningGuideGenerator as _RG
            try:
                guide_generator = _RG()
            except Exception:
                from .cognitive_analysis import ReasoningGuideGenerator as _RB
                guide_generator = _RB()
        except Exception:
            from .cognitive_analysis import ReasoningGuideGenerator as _RB
            guide_generator = _RB()
        
        logger.info(f"Starting background clinical reasoning analysis for session {session_id}")
        
        # Get the session and MCQ
        session = CognitiveReasoningSession.objects.get(id=session_id)
        mcq = MCQ.objects.get(id=mcq_id)
        
        # Update session status to processing
        session.status = CognitiveReasoningSession.PROCESSING
        session.save()
        
        # Try OpenAI analysis path if available; otherwise generate guidance directly
        guidance_steps = []
        analysis = None
        try:
            cognitive_analyzer = getattr(guide_generator, 'cognitive_analyzer', None)
            if cognitive_analyzer:
                analysis = cognitive_analyzer.analyze_reasoning(mcq, selected_answer, user_reasoning, is_correct)
        except Exception:
            analysis = None
        
        if analysis:
            guidance_steps = guide_generator.generate_guidance_from_analysis(analysis)
        else:
            # Fall back to direct guidance generation (rule-based or minimal)
            try:
                guidance_steps = guide_generator.generate_guidance(mcq, selected_answer, user_reasoning, is_correct)
            except Exception:
                guidance_steps = []
        
        # Ensure at least 3 steps for a smooth UI experience
        if len(guidance_steps) < 2:
            from .cognitive_analysis_openai import GuideStep
            # Add a practice check step
            practice_html = (
                "<p><strong>Practice Check:</strong> Try restating the key discriminator(s) in one line and write one question you would ask next.</p>"
            )
            guidance_steps.append(GuideStep(title="📝 Practice Check", content=practice_html))
        if len(guidance_steps) < 3:
            next_html = (
                "<p><strong>Next Steps:</strong></p><ul>"
                "<li>Review the top discriminator between your choice and the correct option.</li>"
                "<li>Create a one‑line algorithm for similar future cases.</li>"
                "</ul>"
            )
            guidance_steps.append(GuideStep(title="➡️ Next Steps", content=next_html))
        
        # Convert guidance steps to JSON format
        steps_json = []
        for step in guidance_steps:
            step_data = {
                'title': step.title,
                'content': step.content,
                'question': step.question,
                'evidence': step.evidence,
                'action': step.action
            }
            steps_json.append(step_data)
        
        # Update session with analysis results
        if analysis:
            session.primary_error = analysis.primary_error.value if analysis.primary_error else None
            session.secondary_errors = [error.value for error in analysis.secondary_errors]
            session.knowledge_gaps = analysis.knowledge_gaps
            session.misconceptions = analysis.misconceptions
            session.reasoning_quality = analysis.reasoning_quality
            session.confidence_score = int(analysis.confidence_level * 100)
        else:
            # Minimal metadata when running without OpenAI
            session.reasoning_quality = session.reasoning_quality or 'fair'
            session.confidence_score = session.confidence_score or 50
        session.guidance_steps = steps_json
        # Ensure current_step is set to the first step to avoid out-of-range issues
        try:
            session.current_step = 0
        except Exception:
            # Field may default to 0 or be absent on older schemas; ignore if not present
            pass
        session.status = CognitiveReasoningSession.READY
        session.completed_at = timezone.now()
        session.save()
        
        logger.info(f"Completed background clinical reasoning analysis for session {session_id}")
        
        return {
            'success': True,
            'session_id': session_id,
            'message': 'Clinical reasoning analysis completed successfully'
        }
        
    except Exception as e:
        logger.error(f"Error in background clinical reasoning analysis: {e}", exc_info=True)
        
        # Update session status to failed
        try:
            session = CognitiveReasoningSession.objects.get(id=session_id)
            session.status = CognitiveReasoningSession.FAILED
            try:
                session.error_message = str(e)
            except Exception:
                pass
            session.save()
        except:
            pass
        
        # Retry the task with exponential backoff
        if self.request.retries < self.max_retries:
            logger.info(f"Retrying task, attempt {self.request.retries + 1}")
            raise self.retry(countdown=60 * (2 ** self.request.retries))
        
        return {
            'success': False,
            'session_id': session_id,
            'error': str(e)
        }


def _ai_job_cache_key(job_id: str) -> str:
    return f"ai_job:{job_id}"


@shared_task(bind=True, max_retries=0)
def run_ai_job(self, job_id: str, action: str, params: dict):
    """Generic AI job runner executed on Celery worker.

    Stores status and result in the Django cache under key ai_job:{job_id}.
    """
    cache_key = _ai_job_cache_key(job_id)

    def update(status: str, **extra):
        payload = cache.get(cache_key) or {}
        payload.update({'status': status, **extra})
        cache.set(cache_key, payload, timeout=3600)

    try:
        update('processing')

        if action == 'ask_gpt':
            from .models import MCQ
            from .openai_integration import answer_question_about_mcq

            mcq = MCQ.objects.get(id=params['mcq_id'])
            question = params.get('question', '')
            answer = answer_question_about_mcq(mcq, question)
            update('ready', result={'answer': answer})
            return

        elif action == 'generate_test_question':
            from .openai_integration import client, DEFAULT_MODEL, chat_completion

            mcq_id = params['mcq_id']
            guidance_content = params.get('guidance_content', '')

            # Prepare prompts (aligned with views.generate_test_question)
            system_prompt = (
                "You are a neurological education expert specializing in creating high-quality assessment questions. "
                "Create a new question that tests deeper understanding of the same concept in the guidance. "
                "Return ONLY valid JSON with keys: question, options, correct_answer, correct_feedback, "
                "incorrect_feedback, detailed_explanation."
            )
            guidance_summary = guidance_content[:1500]
            user_prompt = f"""Based on the following neurological guidance content, create a related test question:\n\n{guidance_summary}\n\nReturn ONLY valid JSON as specified."""

            resp = chat_completion(
                client,
                DEFAULT_MODEL,
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=1200,
                temperature=0.7,
                top_p=0.95,
                response_format={"type": "json_object"},
                timeout=50,
            )
            content = ''
            try:
                content = get_first_choice_text(resp) if resp else ''
            except Exception:
                content = ''
            if not content or not str(content).strip():
                update('failed', error='Empty response while generating test question')
                return
            try:
                data = json.loads(content)
            except Exception:
                # Minimal fallback structure
                data = {
                    'question': 'Unable to generate automatically right now. Try again shortly.',
                    'options': {'A': '', 'B': '', 'C': '', 'D': ''},
                    'correct_answer': 'A',
                    'correct_feedback': 'Correct.',
                    'incorrect_feedback': 'Review the key concept and try again.',
                    'detailed_explanation': ''
                }
            update('ready', result=data)
            return

        elif action == 'analyze_test_reasoning':
            from .openai_integration import client, DEFAULT_MODEL, chat_completion

            question = params.get('question_text', '')
            options_text = params.get('options_text', '')
            correct_answer = params.get('correct_answer', '')
            user_reasoning = params.get('user_reasoning', '')

            system_prompt = (
                "You are a neurological education expert who provides personalized feedback on learners' clinical reasoning. "
                "Return structured HTML suitable for direct display (use <p>, <ul>, <li>, <strong>)."
            )
            user_prompt = f"""Question: {question}\nOptions:\n{options_text}\nCorrect Answer: {correct_answer}\n\nLearner's reasoning:\n{user_reasoning}\n\nProvide detailed, educational feedback with HTML formatting."""

            resp = chat_completion(
                client,
                DEFAULT_MODEL,
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=1000,
                temperature=0.4,
                top_p=0.95,
                timeout=40,
            )
            html = ''
            try:
                html = get_first_choice_text(resp) if resp else ''
            except Exception:
                html = ''
            if not html or not str(html).strip():
                html = '<div class="alert alert-warning">Could not analyze reasoning at the moment. Please try again.</div>'
            update('ready', result={'detailed_feedback': html})
            return

        elif action == 'generate_explanation':
            from .models import MCQ
            from .openai_integration import generate_explanation
            from .views import _process_explanation

            mcq = MCQ.objects.get(id=params['mcq_id'])
            reason = params.get('reason', '')
            raw = generate_explanation(mcq, reason)
            processed = _process_explanation(raw, mcq)
            mcq.explanation = processed
            mcq.save()
            update('ready', result={'mcq_id': mcq.id})
            return

        else:
            update('failed', error=f'Unknown action: {action}')
            return

    except Exception as e:
        logger.error(f"AI job {job_id} failed [{action}]: {e}")
        update('failed', error=str(e))


@shared_task(bind=True, max_retries=2)
def process_mcq_to_case_conversion(self, mcq_id, user_id, tracking_id=None):
    """
    Background task to convert MCQ to case-based learning session
    
    Args:
        mcq_id: MCQ ID to convert
        user_id: User ID requesting the conversion
        tracking_id: Optional tracking ID for comprehensive debugging
    """
    import os
    import time
    from django.core.cache import cache
    from django.db import transaction
    
    try:
        from .models import MCQ, MCQCaseConversionSession
        from .mcq_case_converter import convert_mcq_to_case
        from django.contrib.auth import get_user_model
        from .case_conversion_tracker import conversion_tracker
        
        User = get_user_model()
        
        # Initialize tracking if not provided
        if not tracking_id:
            tracking_id = conversion_tracker.start_conversion_tracking(
                mcq_id=mcq_id,
                user_id=user_id,
                request_source="background_task_direct"
            )
        
        # Log environment info for debugging
        logger.info(f"Starting MCQ-to-Case conversion for MCQ {mcq_id} on dyno: {os.environ.get('DYNO', 'local')}")
        
        # Implement simple distributed lock using cache
        lock_key = f"mcq_conversion_lock_{mcq_id}_{user_id}"
        lock_acquired = cache.add(lock_key, "locked", timeout=120)  # 2 minute lock
        
        if not lock_acquired:
            logger.warning(f"Conversion already in progress for MCQ {mcq_id}, user {user_id}")
            # Wait briefly and check if there's a result
            time.sleep(2)
            existing_session = MCQCaseConversionSession.objects.filter(
                mcq_id=mcq_id,
                user_id=user_id,
                status=MCQCaseConversionSession.READY
            ).order_by('-created_at').first()
            if existing_session:
                return {
                    'success': True,
                    'mcq_id': mcq_id,
                    'session_id': existing_session.id,
                    'message': 'Using existing conversion'
                }
            raise Exception("Another conversion is in progress")
        
        try:
            # Get the MCQ and user with database lock to prevent race conditions
            with transaction.atomic():
                mcq = MCQ.objects.select_for_update().get(id=mcq_id)
                user = User.objects.get(id=user_id)
                
                # Verify MCQ ID matches what we expect
                if mcq.id != mcq_id:
                    raise Exception(f"MCQ ID mismatch: expected {mcq_id}, got {mcq.id}")
        
            # Get the existing session (should have been created by the view)
            try:
                session = MCQCaseConversionSession.objects.filter(
                    mcq=mcq,
                    user=user
                ).order_by('-created_at').first()
                
                if not session:
                    # Fallback: create session if it doesn't exist
                    session = MCQCaseConversionSession.objects.create(
                        mcq=mcq,
                        user=user,
                        status=MCQCaseConversionSession.PROCESSING
                    )
                else:
                    # Update status to processing
                    session.status = MCQCaseConversionSession.PROCESSING
                    session.save()
                    
                # Double-check session MCQ ID
                if session.mcq_id != mcq_id:
                    logger.error(f"Session MCQ ID mismatch! Expected {mcq_id}, got {session.mcq_id}")
                    raise Exception(f"Session data corruption: MCQ ID {session.mcq_id} != {mcq_id}")
                    
            except Exception as e:
                logger.error(f"Error getting conversion session for MCQ {mcq_id}: {e}")
                # Create a new session as fallback with explicit verification
                session = MCQCaseConversionSession.objects.create(
                    mcq=mcq,
                    user=user,
                    status=MCQCaseConversionSession.PROCESSING
                )
                # Verify the created session
                if session.mcq_id != mcq_id:
                    raise Exception(f"Failed to create session with correct MCQ ID")
        
            # Convert MCQ to case (this includes validation and retries)
            try:
                logger.info(f"Converting MCQ {mcq_id} to case...")
                case_data = convert_mcq_to_case(mcq, include_debug=False)
                
                # Log case generation
                conversion_tracker.log_case_generation(
                    tracking_id=tracking_id,
                    case_data=case_data,
                    api_call_details={
                        'include_debug': False,
                        'mcq_id': mcq_id,
                        'conversion_attempt': 1
                    }
                )
                
                # Verify the case data has correct MCQ ID
                if case_data.get('source_mcq_id') != mcq_id:
                    conversion_tracker.log_error(
                        tracking_id, 
                        "CASE_GENERATION_MCQ_ID_MISMATCH", 
                        f"Expected {mcq_id}, got {case_data.get('source_mcq_id')}"
                    )
                    logger.error(f"Case data MCQ ID mismatch! Expected {mcq_id}, got {case_data.get('source_mcq_id')}")
                    raise Exception(f"Generated case has wrong MCQ ID: {case_data.get('source_mcq_id')} != {mcq_id}")
                    
            except Exception as e:
                # If conversion fails, retry with debug mode enabled
                logger.warning(f"Initial conversion failed for MCQ {mcq_id}, retrying with debug mode: {e}")
                try:
                    case_data = convert_mcq_to_case(mcq, include_debug=True)
                    
                    # Verify again
                    if case_data.get('source_mcq_id') != mcq_id:
                        logger.error(f"Debug conversion also has wrong MCQ ID: {case_data.get('source_mcq_id')}")
                        raise Exception(f"Persistent MCQ ID mismatch in conversion")
                        
                except Exception as debug_e:
                    # Store detailed error with debug info
                    session.status = MCQCaseConversionSession.FAILED
                    session.error_message = str(debug_e)
                    session.save()
                    raise debug_e
            
            # Validate generated case with end-to-end integrity and Heroku-aware validation
            import os
            from .end_to_end_integrity import e2e_integrity
            validation_result = e2e_integrity.validate_generated_case(mcq, case_data, session)
            
            # Log validation result
            conversion_tracker.log_validation_result(
                tracking_id=tracking_id,
                validation_result=validation_result,
                validator_type="e2e_integrity"
            )
            
            # On Heroku, be more permissive with validation failures
            is_heroku = bool(os.environ.get('DYNO'))
            
            if not validation_result['valid']:
                # Check if this is a recoverable validation issue on Heroku
                if is_heroku and case_data.get('source_mcq_id') == mcq_id:
                    # Allow cases that have correct MCQ ID but failed semantic validation
                    logger.warning(f"Heroku: Allowing case despite validation warning for MCQ {mcq_id}: {validation_result['reason']}")
                    # Mark as passed with warning
                    validation_result['valid'] = True
                    validation_result['reason'] = f"Heroku override: {validation_result['reason']}"
                else:
                    # Mark session as failed if validation fails critically
                    session.status = MCQCaseConversionSession.FAILED
                    session.error_message = f"Case validation failed: {validation_result['reason']}"
                    session.save()
                    
                    logger.error(f"Case validation failed for MCQ {mcq_id}: {validation_result['reason']}")
                    raise Exception(f"Case validation failed: {validation_result['reason']}")
            
            # Store the validated case data with integrity protection
            e2e_integrity.store_case_with_integrity(session, case_data)
            
            # Log database storage
            conversion_tracker.log_database_storage(
                tracking_id=tracking_id,
                session_id=session.id,
                storage_checksum=case_data.get('_storage_checksum', 'unknown')
            )
            
            logger.info(f"Completed background MCQ-to-Case conversion for MCQ {mcq_id}")
            
            return {
                'success': True,
                'mcq_id': mcq_id,
                'session_id': session.id,
                'message': 'MCQ-to-Case conversion completed successfully',
                'tracking_id': tracking_id
            }
            
        finally:
            # Always release the distributed lock
            try:
                cache.delete(lock_key)
                logger.info(f"Released conversion lock for MCQ {mcq_id}")
            except:
                pass
        
    except Exception as e:
        logger.error(f"Error in background MCQ-to-Case conversion: {e}", exc_info=True)
        
        # Always release the lock on error
        try:
            cache.delete(f"mcq_conversion_lock_{mcq_id}_{user_id}")
        except:
            pass
        
        # Update session status to failed
        try:
            from .models import MCQCaseConversionSession
            session = MCQCaseConversionSession.objects.filter(
                mcq_id=mcq_id, 
                user_id=user_id
            ).first()
            if session:
                session.status = MCQCaseConversionSession.FAILED
                session.error_message = str(e)
                session.save()
        except:
            pass
        
        # Retry the task with exponential backoff
        if self.request.retries < self.max_retries:
            logger.info(f"Retrying MCQ conversion task, attempt {self.request.retries + 1}")
            raise self.retry(countdown=30 * (2 ** self.request.retries))
        
        return {
            'success': False,
            'mcq_id': mcq_id,
            'error': str(e)
        }


@shared_task(bind=True, max_retries=2)
def run_ai_edit_job(self, job_id):
    """
    Execute queued AI editing jobs (question/options/explanation).
    """
    try:
        job = AIEditJob.objects.select_related("mcq").get(id=job_id)
    except AIEditJob.DoesNotExist:
        logger.error("AI edit job %s not found", job_id)
        return

    if job.status not in {AIEditJob.STATUS_PENDING, AIEditJob.STATUS_FAILED}:
        logger.info("AI edit job %s skipped (status=%s)", job_id, job.status)
        return

    job.mark_running()

    payload = job.payload or {}
    mcq_id = payload.get("mcq_id") or getattr(job.mcq, "id", None)

    try:
        mcq = MCQ.objects.get(id=mcq_id)
    except MCQ.DoesNotExist:
        job.mark_failed(f"MCQ {mcq_id} not found.")
        return

    try:
        if job.job_type == AIEditJob.TYPE_QUESTION:
            custom_instructions = payload.get("custom_instructions", "")
            improved_text = ai_edit_question(mcq, custom_instructions)
            job.mark_success(
                {
                    "success": True,
                    "improved_text": improved_text,
                    "message": "AI has generated an improved question",
                }
            )
            return

        if job.job_type == AIEditJob.TYPE_OPTIONS:
            mode = payload.get("mode", "fill_missing")
            custom_instructions = payload.get("custom_instructions", "")
            draft_question_text = payload.get("question_text")
            current_options_override = payload.get("current_options")
            requested_correct_answer = payload.get("correct_answer")
            auto_regenerate = payload.get("auto_regenerate", True)
            auto_apply = payload.get("auto_apply", False)

            if isinstance(draft_question_text, str) and draft_question_text.strip():
                mcq.question_text = draft_question_text

            if isinstance(current_options_override, dict) and current_options_override:
                mcq.options = current_options_override

            if isinstance(requested_correct_answer, str) and requested_correct_answer.strip():
                mcq.correct_answer = requested_correct_answer.strip()

            if mode == "fill_missing":
                improved_options = ai_edit_options(mcq, custom_instructions)
            else:
                improved_options = ai_improve_all_options(mcq, custom_instructions)

            response_data = {
                "success": True,
                "options": improved_options,
                "message": f"AI has {'filled missing' if mode == 'fill_missing' else 'improved all'} options",
            }

            if auto_apply and isinstance(improved_options, dict):
                try:
                    mcq.options = improved_options
                    mcq.save()
                    mcq.refresh_from_db()
                    response_data["auto_applied"] = True

                    if auto_regenerate:
                        try:
                            regenerated_text = regenerate_unified_explanation(mcq)
                            if regenerated_text and isinstance(regenerated_text, str):
                                regenerated_text = regenerated_text.strip()
                                mcq.unified_explanation = regenerated_text
                                mcq.explanation = regenerated_text
                                mcq.explanation_sections = None
                                mcq.save(
                                    update_fields=[
                                        "unified_explanation",
                                        "explanation",
                                        "explanation_sections",
                                    ]
                                )
                                from .explanation_utils import render_explanation_as_html

                                response_data["unified_explanation"] = regenerated_text
                                response_data["html_preview"] = render_explanation_as_html(
                                    regenerated_text
                                )
                                response_data["explanations_regenerated"] = True
                                response_data["message"] += (
                                    ", applied changes, and regenerated explanations"
                                )
                            else:
                                response_data["message"] += (
                                    " and applied changes (explanation regeneration failed)"
                                )
                                response_data["explanations_regenerated"] = False
                                response_data["regeneration_error"] = (
                                    "AI did not return a valid explanation"
                                )
                        except Exception as regen_exc:
                            response_data["message"] += (
                                " and applied changes (explanation regeneration failed)"
                            )
                            response_data["explanations_regenerated"] = False
                            response_data["regeneration_error"] = str(regen_exc)
                except Exception as apply_exc:
                    response_data["auto_applied"] = False
                    response_data["error"] = f"Failed to apply changes: {apply_exc}"

            job.mark_success(response_data)
            return

        if job.job_type == AIEditJob.TYPE_EXPLANATION:
            current_content = payload.get("current_content", "")
            custom_instructions = payload.get("custom_instructions", "")
            target_mode = payload.get("mode", "enhance")
            section_name = payload.get("section_name")
            enhanced_content = ai_edit_explanation_text(
                mcq,
                current_content=current_content,
                custom_instructions=custom_instructions,
                mode=target_mode,
            )
            if not enhanced_content or not isinstance(enhanced_content, str):
                raise ValueError("AI returned empty content for this explanation.")
            job.mark_success(
                {
                    "success": True,
                    "enhanced_content": enhanced_content.strip(),
                    "section_name": section_name,
                    "message": "AI has enhanced the explanation.",
                }
            )
            return

        if job.job_type == AIEditJob.TYPE_EXPLANATION_REGENERATE:
            custom_instructions = payload.get("custom_instructions", "")
            regenerated_text = regenerate_unified_explanation(
                mcq, custom_instructions=custom_instructions
            )

            if not regenerated_text or not regenerated_text.strip():
                raise ValueError("Failed to regenerate the explanation.")

            regenerated_text = regenerated_text.strip()
            mcq.unified_explanation = regenerated_text
            mcq.explanation = regenerated_text
            mcq.explanation_sections = None
            mcq.save(update_fields=["unified_explanation", "explanation", "explanation_sections"])

            from .explanation_utils import render_explanation_as_html

            job.mark_success(
                {
                    "success": True,
                    "message": "Explanation regenerated successfully",
                    "unified_explanation": regenerated_text,
                    "html_preview": render_explanation_as_html(regenerated_text),
                }
            )
            return

        job.mark_failed(f"Unknown job type: {job.job_type}")
    except Exception as exc:
        logger.exception("AI edit job %s failed: %s", job_id, exc)
        job.mark_failed(str(exc))
        if self.request.retries < self.max_retries:
            raise self.retry(countdown=10 * (2 ** self.request.retries))
