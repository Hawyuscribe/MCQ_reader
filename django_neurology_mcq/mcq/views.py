"""
Views for the MCQ application.
Optimized views for better performance and maintainability.
"""

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse, HttpResponse, Http404
from django.contrib import messages
import csv
from django.contrib.auth import login, logout, authenticate
from django.db.models import Count, Q
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.forms import UserCreationForm as DjangoUserCreationForm
from django.contrib.auth.forms import AuthenticationForm as DjangoAuthenticationForm
from django.core.exceptions import PermissionDenied
from .forms import CaseInsensitiveUserCreationForm, CaseInsensitiveAuthenticationForm, QuestionReportForm
from .services import MCQService, BookmarkService, NoteService, FlashcardService, ReasoningService
from .services.case_learning_service import case_conversation_service

from datetime import timedelta
import json
import re
import logging
from django.core.serializers.json import DjangoJSONEncoder

from .models import (
    MCQ,
    Bookmark,
    Flashcard,
    Note,
    ReasoningSession,
    QuestionReport,
    PersistentCaseLearningSession,
    CognitiveReasoningSession,
    MCQCaseConversionSession,
    AdminDebugEvent,
)
try:
    from .models import HiddenMCQ
except ImportError:
    # For backwards compatibility if HiddenMCQ doesn't exist yet
    HiddenMCQ = None

# Deprecated helper preserved for backwards compatibility in templates/imports
def get_hidden_mcqs(user):
    """Wrapper maintained for legacy imports; delegates to service layer."""
    return MCQService.get_hidden_mcq_ids(user)
from .openai_integration import (
    generate_explanation, improve_question, generate_new_options, 
    verify_mcq_answer, answer_question_about_mcq, clinical_reasoning_coach
)
from django.core.cache import cache
import uuid
from .admin_debug import record_admin_debug_event, serialize_events

# Configure logger
logger = logging.getLogger(__name__)

# Subspecialty definitions
SUBSPECIALTIES = [
    "Critical Care Neurology", "Dementia", "Epilepsy", "Headache", 
    "Movement Disorders", "Neuroanatomy", "Neurogenetics", "Neuroimmunology",
    "Neuro-infectious", "Neuro-oncology", "Neuro-otology", "Neuroophthalmology",
    "Neuropsychiatry", "Neurotoxicology", "Neuromuscular", "Pediatric Neurology",
    "Sleep Neurology", "Vascular Neurology/Stroke", "Other/Unclassified"
]

# Mapping between display names and database names
SUBSPECIALTY_MAPPING = {
    # Maps display names (as shown in UI) to actual database subspecialty values
    "Critical Care Neurology": "Critical Care Neurology",
    "Dementia": "Dementia",
    "Epilepsy": "Epilepsy",
    "Headache": "Headache",
    "Movement Disorders": "Movement Disorders",
    "Neuroanatomy": "Neuroanatomy",
    "Neurogenetics": "Neurogenetics",
    "Neuroimmunology": "Neuroimmunology",
    "Neuro-infectious": "Neuro-infectious",
    "Neuro-oncology": "Neuro-oncology", 
    "Neuro-otology": "Neuro-otology",
    "Neuroophthalmology": "Neuroophthalmology",  # Now matches DB exactly
    "Neuropsychiatry": "Neuropsychiatry",
    "Neurotoxicology": "Neurotoxicology",
    "Neuromuscular": "Neuromuscular",
    "Pediatric Neurology": "Pediatric Neurology",
    "Sleep Neurology": "Sleep Neurology",
    "Vascular Neurology/Stroke": "Vascular Neurology/Stroke",  # Now matches DB exactly
    "Other/Unclassified": "Other/Unclassified"  # Now matches DB exactly
}

def index(request):
    """Redirect to the dashboard page."""
    return redirect('dashboard')


def login_view(request):
    """
    Handle user login.
    Authenticates user credentials and creates a session.
    Also checks if the account has expired before allowing login.
    Uses case-insensitive username authentication.
    """
    if request.method == 'POST':
        form = CaseInsensitiveAuthenticationForm(request, data=request.POST)
        if form.is_valid():
            username = form.cleaned_data.get('username')
            password = form.cleaned_data.get('password')
            user = authenticate(username=username, password=password)
            
            if user is not None:
                # Check if the user has a profile and if it's active
                try:
                    from .models import UserProfile
                    
                    # Get or create a profile for the user
                    profile, created = UserProfile.objects.get_or_create(
                        user=user,
                        defaults={
                            'expiration_date': timezone.now() + timedelta(days=30)
                        }
                    )
                    
                    # Check if the account is expired or manually deactivated
                    if not profile.is_active:
                        if profile.is_expired:
                            messages.error(request, "Your account has expired. Please contact an administrator for renewal.")
                        else:
                            messages.error(request, "Your account has been deactivated. Please contact an administrator.")
                        logger.warning(f"Login attempt for expired or inactive account: {username}")
                        return render(request, 'mcq/login.html', {'form': form})
                    
                    # Account is active, proceed with login
                    login(request, user)
                    logger.info(f"User {username} logged in successfully")
                    
                    # Check if account is about to expire
                    if profile.expiration_date and (profile.expiration_date - timezone.now()).days <= 3:
                        days_remaining = max(0, (profile.expiration_date - timezone.now()).days)
                        if days_remaining == 0:
                            hours_remaining = max(0, int((profile.expiration_date - timezone.now()).total_seconds() / 3600))
                            if hours_remaining > 0:
                                messages.warning(request, f"Your account will expire in {hours_remaining} hours. Please contact an administrator for renewal.")
                            else:
                                messages.warning(request, "Your account is about to expire. Please contact an administrator for renewal immediately.")
                        else:
                            messages.warning(request, f"Your account will expire in {days_remaining} days. Please contact an administrator for renewal.")
                    
                    return redirect('dashboard')
                    
                except Exception as e:
                    # If there's an error checking the profile, log the error but allow login
                    logger.error(f"Error checking profile for {username}: {str(e)}")
                    login(request, user)
                    return redirect('dashboard')
            else:
                # Authentication failed
                messages.error(request, "Invalid username or password.")
                logger.warning(f"Failed login attempt for username: {username}")
        else:
            # Form validation failed
            messages.error(request, "Invalid username or password.")
            logger.warning(f"Failed login attempt for invalid form data: {request.POST.get('username')}")
            
    form = CaseInsensitiveAuthenticationForm()
    return render(request, 'mcq/login.html', {'form': form})


def register_view(request):
    """
    Handle user registration.
    Creates a new user account and logs the user in.
    Uses case-insensitive username validation to prevent duplicate usernames.
    """
    if request.method == 'POST':
        form = CaseInsensitiveUserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            logger.info(f"New user registered: {user.username}")
            messages.success(request, "Registration successful!")
            return redirect('dashboard')
        else:
            logger.warning(f"Registration failed: {form.errors}")
            messages.error(request, "Registration failed. Please correct the errors.")
    else:
        form = CaseInsensitiveUserCreationForm()

    return render(request, 'mcq/register.html', {'form': form})


def logout_view(request):
    """
    Handle user logout.
    Terminates the current user session.
    """
    if request.user.is_authenticated:
        logger.info(f"User {request.user.username} logged out")
    logout(request)
    return redirect('login')

@login_required
def dashboard(request):
    """
    User dashboard showing MCQ statistics, flashcards due, and bookmarks.
    Optimized for performance with select_related and fewer queries.
    Also handles mock examination configuration.
    """
    # Check if this is a mock exam form submission
    if request.method == 'POST' and 'exam_type' in request.POST:
        # Extract mock exam configuration
        exam_type = request.POST.get('exam_type', 'mixed')
        mcq_count_option = request.POST.get('mcq_count_option', 'limited')
        mcq_count = int(request.POST.get('mcq_count', 20)) if mcq_count_option == 'limited' else 0  # 0 indicates all MCQs
        time_limit = int(request.POST.get('time_limit', 60))
        display_options = request.POST.get('display_options', 'all')
        start_year = request.POST.get('start_year', '')
        end_year = request.POST.get('end_year', '')
        subspecialties = request.POST.getlist('subspecialties', [])
        
        # Generate a unique exam ID
        import uuid
        exam_id = str(uuid.uuid4())
        
        # Get hidden MCQs for this user
        hidden_mcqs = get_hidden_mcqs(request.user)
        
        # Get MCQs for the selected subspecialties, excluding hidden ones
        from django.db.models import Q
        query = MCQ.objects.filter(subspecialty__in=subspecialties).exclude(id__in=hidden_mcqs)
        
        # If no subspecialties selected, use all
        if not subspecialties:
            query = MCQ.objects.all().exclude(id__in=hidden_mcqs)
        
        # Filter by exam type if not 'mixed'
        if exam_type != 'mixed':
            query = query.filter(Q(exam_type=exam_type) | Q(exam_type__isnull=True))

        # Apply year range filter if provided
        if start_year and end_year:
            try:
                year_filter = Q(
                    # Include MCQs where year is in range
                    Q(exam_year__gte=int(start_year), exam_year__lte=int(end_year)) |
                    # OR include MCQs where year is NULL
                    Q(exam_year__isnull=True)
                )
                query = query.filter(year_filter)
                logger.info(f"Filtering MCQs by year range: {start_year}-{end_year}")
            except (ValueError, TypeError):
                # Handle case where start_year or end_year cannot be converted to int
                logger.warning(f"Invalid year range: {start_year}-{end_year}")
        
        # Randomly select MCQs up to the requested count
        import random
        all_mcqs = list(query)
        
        # Handle the MCQ count options
        if mcq_count_option == 'all' or mcq_count == 0:
            # Use all MCQs that match the criteria
            selected_mcqs = all_mcqs
            messages.info(request, f"Using all {len(all_mcqs)} MCQs that match your criteria.")
        else:
            # Check if we have enough MCQs for the requested count
            if len(all_mcqs) < mcq_count:
                messages.warning(request, f"Not enough MCQs available. Using all {len(all_mcqs)} available MCQs.")
                selected_mcqs = all_mcqs
            else:
                # Randomly select MCQs up to the requested count
                selected_mcqs = random.sample(all_mcqs, mcq_count)
        
        # Process MCQ options for each selected MCQ
        for mcq in selected_mcqs:
            if mcq.options and isinstance(mcq.options, str):
                try:
                    import json
                    mcq.options = json.loads(mcq.options)
                except json.JSONDecodeError:
                    # If options can't be parsed as JSON, keep as is
                    pass
        
        # Save exam configuration in session
        request.session['mock_exam'] = {
            'exam_id': exam_id,
            'mcq_ids': [mcq.id for mcq in selected_mcqs],
            'time_limit': time_limit,
            'display_options': display_options,
            'exam_type': exam_type,
            'subspecialties': subspecialties,
            'start_year': start_year,
            'end_year': end_year,
            'start_time': timezone.now().isoformat(),
        }
        
        # Prepare context for the template
        context = {
            'exam_id': exam_id,
            'mcqs': selected_mcqs,
            'time_limit': time_limit,
            'display_options': display_options,
            'exam_type': exam_type,
            'subspecialties': subspecialties,
            'start_year': start_year,
            'end_year': end_year,
        }
        
        # Render the mock exam page
        return render(request, 'mcq/mock_exam.html', context)
    
    # Get hidden MCQs for this user (used in multiple queries)
    hidden_mcqs = get_hidden_mcqs(request.user)
    
    # Get hidden MCQ count
    hidden_mcq_count = len(hidden_mcqs) if hidden_mcqs else 0
    
    # Get subspecialty statistics in one query
    subspecialty_stats = get_subspecialty_stats(request.user, hidden_mcqs)
    
    # Define time ranges for flashcards
    today = timezone.now()
    week_end = today + timedelta(days=7)
    
    # Get flashcards with optimized queries
    flashcards_due_today, flashcards_due_this_week = get_flashcards_for_user(
        request.user, today, week_end, hidden_mcqs
    )
    
    # Get bookmarked MCQs with one optimized query
    bookmarked_mcqs = Bookmark.objects.filter(user=request.user).exclude(
        mcq_id__in=hidden_mcqs
    ).select_related('mcq').order_by('mcq__id')
    
    # If user is admin, get pending reports count
    pending_reports_count = 0
    if request.user.is_staff:
        pending_reports_count = QuestionReport.objects.filter(status='pending').count()
    
    state_labels = {
        0: 'Starting Case',
        1: 'History Taking',
        2: 'History Review',
        3: 'Physical Examination',
        4: 'Examination Review',
        5: 'Your Localization',
        6: 'Localization Feedback',
        7: 'Expert Localization',
        8: 'Your Investigations',
        9: 'Investigation Feedback',
        10: 'Expert Investigations',
        11: 'Your Differential',
        12: 'Differential Feedback',
        13: 'Expert Differential',
        14: 'Your Management',
        15: 'Management Feedback',
        16: 'Expert Management',
        17: 'Conclusion',
        18: 'Follow-Up',
    }

    recent_sessions_qs = (
        PersistentCaseLearningSession.objects.filter(
            user=request.user,
            archived=False,
            completed=False,
        )
        .order_by('-last_activity')[:5]
    )

    recent_case_sessions = []
    for session in recent_sessions_qs:
        case_data = session.case_data or {}
        recent_case_sessions.append(
            {
                'session_id': session.session_id,
                'specialty': session.specialty or 'General Neurology',
                'difficulty': (session.difficulty or 'random').title(),
                'state_label': state_labels.get(session.state, 'In Progress'),
                'last_activity': session.last_activity,
                'is_fallback': bool(case_data.get('is_fallback')),
            }
        )

    # Prepare context for template
    context = {
        'subspecialty_stats': subspecialty_stats,
        'flashcards_due': flashcards_due_today.count(),
        'flashcards_due_week': flashcards_due_today.count() + flashcards_due_this_week.count(),
        'flashcards_due_week_only': flashcards_due_this_week.count(),  # For chart calculation
        'bookmarked_count': bookmarked_mcqs.count(),
        'hidden_mcq_count': hidden_mcq_count,
        'bookmarked_mcqs': bookmarked_mcqs,
        'pending_reports_count': pending_reports_count,
        'recent_case_sessions': recent_case_sessions,
    }
    
    return render(request, 'mcq/dashboard.html', context)


def get_subspecialty_stats(user, hidden_mcqs=None):
    """
    Helper function to get subspecialty statistics.
    Returns statistics for each subspecialty including progress tracking.
    
    Args:
        user: The user to get statistics for
        hidden_mcqs: Optional list of hidden MCQ IDs to exclude
    
    Returns:
        List of dictionaries with subspecialty statistics
    """
    # Get all subspecialties with counts, excluding hidden MCQs
    query = MCQ.objects
    if hidden_mcqs is not None:
        query = query.exclude(id__in=hidden_mcqs)
    
    subspecialty_counts = query.values('subspecialty').annotate(total=Count('id'))
    count_dict = {item['subspecialty']: item['total'] for item in subspecialty_counts}
    
    # Get flashcard counts for the current user
    flashcard_counts = Flashcard.objects.filter(user=user).values(
        'mcq__subspecialty').annotate(completed=Count('id'))
    completed_dict = {item['mcq__subspecialty']: item['completed'] for item in flashcard_counts}
    
    # Build stats for each subspecialty
    subspecialty_stats = []
    for display_name in SUBSPECIALTIES:
        # Use the database name from mapping if it exists
        db_name = SUBSPECIALTY_MAPPING.get(display_name, display_name)
        
        total = count_dict.get(db_name, 0)
        completed = completed_dict.get(db_name, 0)
        
        # Ensure we don't divide by zero
        percentage = round((completed / total * 100), 1) if total > 0 else 0
        
        subspecialty_stats.append({
            'name': display_name,  # Display name for UI
            'db_name': db_name,    # DB name for queries
            'total': total,
            'completed': completed,
            'percentage': percentage
        })
    
    return subspecialty_stats


def get_flashcards_for_user(user, today, week_end, hidden_mcqs=None):
    """
    Helper function to get flashcards due for a user.
    Returns two querysets: flashcards due today and flashcards due this week.
    
    Args:
        user: The user to get flashcards for
        today: Datetime object representing today
        week_end: Datetime object representing the end of the week
        hidden_mcqs: Optional list of hidden MCQ IDs to exclude
    
    Returns:
        Tuple of (flashcards_due_today, flashcards_due_this_week)
    """
    # Base query for all flashcards
    base_query = Flashcard.objects.filter(user=user)
    
    # Exclude hidden MCQs if provided
    if hidden_mcqs is not None:
        base_query = base_query.exclude(mcq_id__in=hidden_mcqs)
    
    # Get flashcards due today with proper select_related for performance
    flashcards_due_today = base_query.filter(
        next_review__lte=today
    ).select_related('mcq').order_by('next_review')
    
    # Get flashcards due later this week (but not today)
    flashcards_due_this_week = base_query.filter(
        next_review__lte=week_end,
        next_review__gt=today
    ).select_related('mcq').order_by('next_review')
    
    return flashcards_due_today, flashcards_due_this_week

@login_required
def test_weakness(request):
    """
    Test the user's knowledge on topics they previously struggled with.
    Always generates a new question based on concepts the user previously struggled with.
    Generated questions are kept in session only and not saved to the database.

    Returns:
        Rendered test weakness page
    """
    # Get an unresolved weakness (incorrect answer) for this user
    from .models import IncorrectAnswer, MCQ
    from django.db.models import Max, Min
    import json

    # Note: We leave temp_weakness_mcq_id in the session for the middleware to handle cleanup
    # The middleware will clean up when navigating away from this view
    # But we still want to ensure we don't create multiple temporary MCQs
    if 'temp_weakness_mcq_id' in request.session:
        temp_mcq_id = request.session['temp_weakness_mcq_id']
        logger.info(f"Will replace existing temporary weakness test MCQ {temp_mcq_id}")

    # Get unresolved incorrect answers, ordered by least recently tested
    unresolved_incorrects = IncorrectAnswer.objects.filter(
        user=request.user,
        resolved=False
    ).order_by('last_tested', '-created_at')

    # If no unresolved weaknesses found, render the page with a message
    if not unresolved_incorrects.exists():
        return render(request, 'mcq/test_weakness.html', {
            'no_weaknesses': True,
            'mcq': None
        })

    # Get the first weakness to test
    weakness = unresolved_incorrects.first()
    original_mcq = weakness.mcq

    # Update last_tested time
    weakness.last_tested = timezone.now()
    weakness.save()

    logger.info(f"Generating new test for weakness from MCQ {original_mcq.id} for user {request.user.username}")

    try:
        # Import OpenAI integration
        from .openai_integration import generate_test_for_weakness, client

        if not client:
            # Fall back to using the original MCQ if OpenAI is not available
            logger.warning(f"OpenAI API not available, using original MCQ for weakness test")
            mcq = original_mcq
            request.session['temp_weakness_mcq_id'] = None
        else:
            # Generate a new question based on the original MCQ
            logger.info(f"Generating new weakness test question for MCQ {original_mcq.id} " +
                       f"for user {request.user.username}")

            # Gather context for the AI
            original_subspecialty = original_mcq.subspecialty
            original_question = original_mcq.question_text
            original_options = original_mcq.get_options_dict()
            original_answer = weakness.selected_answer  # The incorrect answer user selected
            correct_answer = original_mcq.correct_answer
            original_explanation = original_mcq.explanation or "No explanation available."

            # Generate new question
            new_question_data = generate_test_for_weakness(
                original_question=original_question,
                original_options=original_options,
                original_correct=correct_answer,
                user_incorrect=original_answer,
                explanation=original_explanation
            )

            # Create new MCQ object with a timestamp to ensure uniqueness
            import uuid
            unique_id = str(uuid.uuid4())[:8]

            # Add a special tag to mark this as temporary
            mcq = MCQ.objects.create(
                question_number=f"TEMP_WT{original_mcq.id}_{unique_id}",
                question_text=new_question_data['question'],
                options=new_question_data['options'],
                correct_answer=new_question_data['correct_answer'],
                subspecialty=original_subspecialty,
                source_file=f"TEMP_WEAKNESS_TEST_{unique_id}",
                explanation=new_question_data['explanation']
            )

            # Store the temporary MCQ ID in the session
            request.session['temp_weakness_mcq_id'] = mcq.id

            logger.info(f"Created temporary weakness test MCQ {mcq.id} based on MCQ {original_mcq.id}")
    except Exception as e:
        # Log the error and fall back to the original MCQ
        logger.error(f"Error generating weakness test question: {str(e)}", exc_info=True)
        mcq = original_mcq
        request.session['temp_weakness_mcq_id'] = None

    # Render the template with the test question
    return render(request, 'mcq/test_weakness.html', {
        'mcq': mcq,
        'original_mcq': original_mcq,
        'original_answer': weakness.selected_answer
    })

@login_required
@require_POST
def check_weakness_answer(request, mcq_id):
    """
    Check the answer for a weakness test question.
    If correct, mark the original weakness as resolved.
    Cleans up temporary MCQs after checking the answer.

    Returns:
        JSON response with result and explanation
    """
    mcq = get_object_or_404(MCQ, id=mcq_id)
    selected_answer = request.POST.get('answer')
    original_mcq_id = request.POST.get('original_mcq_id')

    # Track if this is a temporary MCQ that we'll need to delete
    is_temp = mcq.source_file and mcq.source_file.startswith('TEMP_WEAKNESS_TEST_')

    # Check if the answer is correct
    is_correct = selected_answer == mcq.correct_answer

    # If answer is correct, mark the weakness as resolved
    if is_correct and original_mcq_id:
        from .models import IncorrectAnswer, MCQ

        try:
            original_mcq = MCQ.objects.get(id=original_mcq_id)

            # Mark all incorrect answers for this MCQ as resolved
            updated = IncorrectAnswer.objects.filter(
                user=request.user,
                mcq=original_mcq,
                resolved=False
            ).update(resolved=True)

            if updated:
                logger.info(f"User {request.user.username} resolved weakness for MCQ {original_mcq_id}")
        except MCQ.DoesNotExist:
            logger.warning(f"Original MCQ {original_mcq_id} not found for weakness test")

    # Get explanation with additional context
    explanation = mcq.explanation or "No detailed explanation available."

    # Use OpenAI to generate a tailored concept explanation if explanation is missing
    if not mcq.explanation or len(mcq.explanation) < 100:
        try:
            from .openai_integration import generate_concept_explanation, client

            if client:
                # Generate concept explanation
                concept_explanation = generate_concept_explanation(
                    question=mcq.question_text,
                    correct_answer=mcq.correct_answer,
                    user_answer=selected_answer
                )
            else:
                concept_explanation = "AI-generated concept explanation unavailable."
        except Exception as e:
            logger.error(f"Error generating concept explanation: {str(e)}")
            concept_explanation = "Error generating concept explanation."
    else:
        # Extract concept explanation from the full explanation
        concept_explanation = "Please review the explanation above for understanding the core concepts."

    # Store the ID in a variable before potentially deleting the MCQ
    mcq_id_for_json = mcq.id
    correct_answer = mcq.correct_answer

    # Mark temporary MCQs for deletion after response - this will be handled by middleware
    if is_temp:
        try:
            # Schedule for deletion after the response is sent
            request.session['mcq_pending_deletion'] = mcq.id
            logger.info(f"Scheduled temporary weakness test MCQ {mcq.id} for deletion via middleware")

            # We keep temp_weakness_mcq_id in the session to allow the middleware to clean it up
            # if the user navigates directly to another page without using our AJAX response
        except Exception as e:
            logger.error(f"Error marking MCQ for cleanup: {str(e)}")

    # Return the results as JSON
    return JsonResponse({
        'is_correct': is_correct,
        'correct_answer': correct_answer,
        'explanation': explanation,
        'concept_explanation': concept_explanation
    })

@login_required
@require_POST
def submit_exam(request):
    """
    Handle the submission of a mock examination.
    Process the answers and calculate the score.
    
    Returns:
        Rendered exam results page
    """
    # Get exam configuration from session
    exam_config = request.session.get('mock_exam', {})
    if not exam_config:
        messages.error(request, "Exam session expired or not found.")
        return redirect('dashboard')
    
    # Extract data from exam configuration
    exam_id = exam_config.get('exam_id')
    mcq_ids = exam_config.get('mcq_ids', [])
    start_time = exam_config.get('start_time')
    time_limit = exam_config.get('time_limit', 60)
    
    # Calculate elapsed time
    from datetime import datetime
    import pytz
    
    start_datetime = datetime.fromisoformat(start_time)
    if start_datetime.tzinfo is None:
        start_datetime = pytz.UTC.localize(start_datetime)
    
    end_datetime = timezone.now()
    elapsed_time = (end_datetime - start_datetime).total_seconds() / 60  # minutes
    
    # Check if time limit was exceeded
    time_exceeded = elapsed_time > time_limit
    
    # Get MCQs used in the exam
    mcqs = MCQ.objects.filter(id__in=mcq_ids)
    
    # Process answers and calculate score
    correct_count = 0
    total_answered = 0
    results = []
    
    for mcq in mcqs:
        answer_key = f"answer-{mcq.id}"
        user_answer = request.POST.get(answer_key, None)
        
        is_correct = False
        if user_answer:
            total_answered += 1
            is_correct = (user_answer == mcq.correct_answer)
            if is_correct:
                correct_count += 1
        
        # Save result for this MCQ
        results.append({
            'mcq': mcq,
            'user_answer': user_answer,
            'correct_answer': mcq.correct_answer,
            'is_correct': is_correct,
            'is_answered': bool(user_answer)
        })
    
    # Calculate percentage score
    score_percentage = round((correct_count / len(mcqs)) * 100, 1) if mcqs else 0
    
    # Determine pass/fail status (70% threshold)
    passed = score_percentage >= 70
    
    # Group results by subspecialty for analysis
    subspecialty_results = {}
    for result in results:
        mcq = result['mcq']
        if mcq.subspecialty not in subspecialty_results:
            subspecialty_results[mcq.subspecialty] = {
                'total': 0,
                'correct': 0,
                'percentage': 0
            }
        
        subspecialty_results[mcq.subspecialty]['total'] += 1
        if result['is_correct']:
            subspecialty_results[mcq.subspecialty]['correct'] += 1
    
    # Calculate percentage for each subspecialty
    for subspecialty in subspecialty_results:
        total = subspecialty_results[subspecialty]['total']
        correct = subspecialty_results[subspecialty]['correct']
        percentage = round((correct / total) * 100, 1) if total > 0 else 0
        subspecialty_results[subspecialty]['percentage'] = percentage
    
    # Clean up session
    if 'mock_exam' in request.session:
        del request.session['mock_exam']
    
    # Context for exam results page
    context = {
        'exam_id': exam_id,
        'results': results,
        'total_mcqs': len(mcqs),
        'total_answered': total_answered,
        'correct_count': correct_count,
        'score_percentage': score_percentage,
        'passed': passed,
        'subspecialty_results': subspecialty_results,
        'elapsed_time': round(elapsed_time, 1),
        'time_limit': time_limit,
        'time_exceeded': time_exceeded,
    }
    
    return render(request, 'mcq/exam_results.html', context)

@staff_member_required
def openai_selftest(request):
    """
    Staff-only diagnostics: exercise a minimal OpenAI chat call using the configured model.
    Returns JSON with model, API key presence, connectivity, and any error message.
    """
    import os
    import time
    from .openai_integration import client as openai_client, get_first_choice_text
    model = os.environ.get('OPENAI_MODEL', 'gpt-5-mini')

    result = {
        'env_model': model,
        'api_key_present': bool(os.environ.get('OPENAI_API_KEY') or os.environ.get('OPENAI_KEY')),
        'client_initialized': bool(openai_client is not None),
        'chat_ok': False,
        'error': None,
        'duration_sec': None,
        'sample': None,
    }

    if not openai_client:
        result['error'] = 'OpenAI client not initialized (missing/invalid API key or package)'
        return JsonResponse(result, status=500)

    start = time.time()
    try:
        # gpt-5 models require 'max_completion_tokens' instead of 'max_tokens'
        token_param = {'max_completion_tokens': 8} if model.startswith('gpt-5') else {'max_tokens': 8}

        # Build params; GPT-5 does not allow overriding temperature (must use default)
        params = {
            'model': model,
            'messages': [{"role": "user", "content": "ping"}],
            'timeout': 20,
            **token_param,
        }
        if not model.startswith('gpt-5'):
            # Safe to include sampling params for pre-GPT-5 models
            params.update({'temperature': 0, 'top_p': 1})

        resp = openai_client.chat.completions.create(**params)
        result['duration_sec'] = round(time.time() - start, 3)
        content = get_first_choice_text(resp) if resp else ''
        result['chat_ok'] = True
        result['sample'] = (content or '')[:120]
        return JsonResponse(result)
    except Exception as e:
        # Capture error and timing info for diagnostics
        result['duration_sec'] = round(time.time() - start, 3)
        result['error'] = f"{type(e).__name__}: {str(e)}"
        return JsonResponse(result, status=502)


@csrf_exempt
def debug_run_clinical_reasoning(request):
    """Staff-or-token debug endpoint to run clinical reasoning inline.
    POST params: mcq_id, selected_answer, is_correct (true/false), user_reasoning.
    Auth: staff user OR header X-Debug-Token matching DEBUG_API_TOKEN env.
    Returns JSON with session_id, html (coaching note), model and duration.
    """
    import os
    import time
    from django.utils.timezone import now
    from .models import MCQ, CognitiveReasoningSession
    # Auth: staff or token
    token = request.headers.get('X-Debug-Token', '')
    if not (getattr(request.user, 'is_staff', False) or (token and token == os.environ.get('DEBUG_API_TOKEN', ''))):
        return JsonResponse({'error': 'Forbidden'}, status=403)

    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    try:
        mcq_id = int(request.POST.get('mcq_id') or 0)
        selected_answer = (request.POST.get('selected_answer') or '').strip()[:2]
        is_correct = str(request.POST.get('is_correct') or '').lower() in ('true', '1', 'yes')
        user_reasoning = (request.POST.get('user_reasoning') or '').strip()
        if not (mcq_id and selected_answer and user_reasoning):
            return JsonResponse({'error': 'Missing required fields'}, status=400)

        mcq = get_object_or_404(MCQ, id=mcq_id)
        from .cognitive_analysis_openai import ReasoningGuideGenerator
        rg = ReasoningGuideGenerator()

        t0 = time.time()
        steps = rg.generate_guidance(mcq, selected_answer, user_reasoning, is_correct)
        dt = round(time.time() - t0, 3)
        html = steps[0].content if steps else ''

        # Persist as a CognitiveReasoningSession for later reference
        # Ensure a valid user_id for NOT NULL constraint by picking a staff user when unauthenticated
        from django.contrib.auth import get_user_model
        _user = request.user if getattr(request.user, 'is_authenticated', False) else None
        if not _user:
            User = get_user_model()
            _user = User.objects.filter(is_staff=True).order_by('id').first() or User.objects.order_by('id').first()
        session = CognitiveReasoningSession.objects.create(
            user=_user,
            mcq=mcq,
            selected_answer=selected_answer,
            is_correct=is_correct,
            user_reasoning=user_reasoning,
            status=CognitiveReasoningSession.READY,
            guidance_steps=[{'title': steps[0].title, 'content': html}] if steps else [],
            reasoning_quality='fair',
            confidence_score=0.5,
            completed_at=now(),
        )

        # Report model used
        from .clinical_reasoning_prompt import OPENAI_CONFIG
        model = OPENAI_CONFIG.get('model')

        return JsonResponse({
            'success': True,
            'session_id': session.id,
            'html': html,
            'html_length': len(html or ''),
            'duration_sec': dt,
            'model': model,
        })
    except Exception as e:
        logger.error(f"debug_run_clinical_reasoning error: {e}", exc_info=True)
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


def debug_reasoning_session(request, session_id):
    """Staff-or-token debug endpoint to fetch a stored reasoning session summary."""
    import os
    token = request.headers.get('X-Debug-Token', '')
    if not (getattr(request.user, 'is_staff', False) or (token and token == os.environ.get('DEBUG_API_TOKEN', ''))):
        return JsonResponse({'error': 'Forbidden'}, status=403)

    try:
        from .models import CognitiveReasoningSession
        s = get_object_or_404(CognitiveReasoningSession, id=session_id)
        return JsonResponse({
            'session': {
                'id': s.id,
                'mcq_id': s.mcq_id,
                'status': s.status,
                'reasoning_quality': s.reasoning_quality,
                'confidence_score': s.confidence_score,
            },
            'guidance_steps_len': len(s.guidance_steps or []),
            'first_step': (s.guidance_steps or [None])[0],
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

@login_required
def search(request):
    """
    Search MCQs by query string with improved accuracy.
    Searches question text, options values (not keys), and explanations with case-insensitive matching.
    Supports searching for multiple words (finds MCQs containing all words regardless of order).

    Args:
        query: Search term for filtering MCQs

    Returns:
        Rendered search results page
    """
    query = request.GET.get('query', '').strip()

    if not query:
        return render(request, 'mcq/search_results.html', {
            'results': [],
            'query': '',
            'result_count': 0
        })

    # Get hidden MCQs for this user
    hidden_mcqs = get_hidden_mcqs(request.user)

    # Split the query into individual words
    search_words = query.split()

    logger.info(f"Performing search for '{query}' with words: {search_words}")

    # Get all MCQs excluding hidden ones (limited to 1000 for performance)
    all_mcqs = MCQ.objects.all().exclude(id__in=hidden_mcqs).order_by('-id')[:1000]

    # Perform a more accurate search
    matching_mcqs = []

    for mcq in all_mcqs:
        # Create searchable text from question and explanation
        searchable_parts = [mcq.question_text.lower()]

        # Include metadata fields so users can search by question number, exam year, etc.
        if mcq.question_number:
            searchable_parts.append(str(mcq.question_number).lower())
        if mcq.exam_year:
            searchable_parts.append(str(mcq.exam_year).lower())
        if mcq.exam_type:
            searchable_parts.append(str(mcq.exam_type).lower())
        if mcq.subspecialty:
            searchable_parts.append(str(mcq.subspecialty).lower())
        if mcq.source_file:
            searchable_parts.append(str(mcq.source_file).lower())

        # Process options - get the option values (not keys)
        if mcq.options:
            # Handle both dict and string JSON formats
            options_dict = {}
            if isinstance(mcq.options, dict):
                options_dict = mcq.options
            elif isinstance(mcq.options, str):
                try:
                    import json
                    options_dict = json.loads(mcq.options)
                except:
                    # If parsing fails, try using the raw string
                    searchable_parts.append(mcq.options.lower())
                    options_dict = {}

            # Add each option value to searchable parts
            for option_value in options_dict.values():
                if isinstance(option_value, str):
                    searchable_parts.append(option_value.lower())

        # Add explanation if it exists
        if mcq.explanation:
            searchable_parts.append(mcq.explanation.lower())

        # Combine all parts for searching
        searchable_text = " ".join(searchable_parts)

        # Check if all search words are in the searchable text
        all_words_found = True
        for word in search_words:
            if word.lower() not in searchable_text:
                all_words_found = False
                break

        if all_words_found:
            matching_mcqs.append(mcq)

    # Sort results by ID (newest first) and limit to 100
    results = matching_mcqs[:100]

    result_count = len(results)
    logger.info(f"Search query '{query}' by {request.user.username} returned {result_count} results")

    return render(request, 'mcq/search_results.html', {
        'results': results,
        'query': query,
        'result_count': result_count
    })

@login_required
def subspecialty_view(request, subspecialty):
    """
    Display MCQs for a specific subspecialty with optional filtering.
    
    Args:
        subspecialty: The subspecialty to filter by
        exam_type: (GET) Optional exam type filter
        start_year: (GET) Optional start year for filtering
        end_year: (GET) Optional end year for filtering
        
    Returns:
        Rendered subspecialty page with filtered MCQs
    """
    # Get filter parameters from request
    exam_type = request.GET.get('exam_type', 'All Types')
    start_year = request.GET.get('start_year', '2018')
    end_year = request.GET.get('end_year', '2024')
    
    # URL decode the subspecialty
    from urllib.parse import unquote
    subspecialty = unquote(subspecialty)
    
    # Convert display name to database name if needed
    db_subspecialty = SUBSPECIALTY_MAPPING.get(subspecialty, subspecialty)
    
    # Log subspecialty access
    logger.info(f"User {request.user.username} accessing subspecialty: '{db_subspecialty}'")
    
    # Get hidden MCQs for this user
    hidden_mcqs = get_hidden_mcqs(request.user)
    
    # Build the query efficiently
    mcqs = get_filtered_mcqs(
        subspecialty=db_subspecialty,
        exam_type=exam_type,
        start_year=start_year,
        end_year=end_year,
        hidden_mcqs=hidden_mcqs
    )
    
    # Count total MCQs in this subspecialty (without filters and excluding hidden)
    total_mcqs = MCQ.objects.filter(subspecialty=db_subspecialty).exclude(id__in=hidden_mcqs).count()
    filtered_count = mcqs.count()
    
    context = {
        'subspecialty': subspecialty,
        'mcqs': mcqs,
        'exam_type': exam_type,
        'start_year': start_year,
        'end_year': end_year,
        'total_mcqs': total_mcqs,
        'filtered_count': filtered_count
    }
    
    return render(request, 'mcq/subspecialty.html', context)


def get_filtered_mcqs(subspecialty, exam_type=None, start_year=None, end_year=None, hidden_mcqs=None):
    """
    Helper function to get filtered MCQs.
    Centralizes MCQ filtering logic for reuse in different views.
    
    Args:
        subspecialty: Subspecialty to filter by
        exam_type: Optional exam type filter
        start_year: Optional start year for filtering
        end_year: Optional end year for filtering
        hidden_mcqs: Optional list of MCQ IDs to exclude
        
    Returns:
        QuerySet of filtered MCQs
    """
    # Base query
    query = MCQ.objects.filter(subspecialty=subspecialty)
    
    # Exclude hidden MCQs if provided
    if hidden_mcqs is not None:
        query = query.exclude(id__in=hidden_mcqs)
    
    # Apply exam type filter if provided
    if exam_type and exam_type != 'All Types':
        # Map old exam types to new ones
        exam_type_mapping = {
            'Promotion': 'Basic level',
            'Part I': 'Advanced', 
            'Part II': 'Board-level',
            'Basic level': 'Basic level',
            'Advanced': 'Advanced',
            'Board-level': 'Board-level'
        }
        
        # Get the standardized exam type
        standardized_type = exam_type_mapping.get(exam_type, exam_type)
        
        # Get old name for backward compatibility
        reverse_mapping = {
            'Basic level': 'Promotion',
            'Advanced': 'Part I',
            'Board-level': 'Part II'
        }
        old_type = reverse_mapping.get(standardized_type, '')
        
        # For exam_type, include matching values (both old and new names) and NULL values
        from django.db.models import Q
        if old_type:
            query = query.filter(
                Q(exam_type=standardized_type) | 
                Q(exam_type=old_type) | 
                Q(exam_type__isnull=True)
            )
        else:
            query = query.filter(Q(exam_type=exam_type) | Q(exam_type__isnull=True))
    
    # Apply year range filter if provided, but include NULL values
    if start_year and end_year:
        try:
            from django.db.models import Q
            year_filter = Q(
                # Include MCQs where year is in range
                Q(exam_year__gte=int(start_year), exam_year__lte=int(end_year)) |
                # OR include MCQs where year is NULL
                Q(exam_year__isnull=True)
            )
            query = query.filter(year_filter)
        except (ValueError, TypeError):
            # Handle case where start_year or end_year cannot be converted to int
            logger.warning(f"Invalid year range: {start_year}-{end_year}")
    
    # Order by ID for consistent ordering
    return query.order_by('id')

@login_required
def view_mcq(request, mcq_id):
    """
    Display a single MCQ with all its details.
    Includes fallback handling for legacy MCQ IDs.
    """
    try:
        mcq = MCQ.objects.get(id=mcq_id)
    except MCQ.DoesNotExist:
        # Handle legacy IDs - check if this might be a legacy ID that needs mapping
        if mcq_id > 100000000:  # Suspiciously large ID
            # Try to find MCQ by question_number pattern
            legacy_pattern = str(mcq_id)[-4:]  # Get last 4 digits
            try:
                # Look for MCQs with question numbers containing these digits
                from django.db import models
                possible_mcqs = MCQ.objects.filter(
                    models.Q(question_number__icontains=legacy_pattern) |
                    models.Q(id__icontains=legacy_pattern)
                ).order_by('id')
                
                if possible_mcqs.exists():
                    # Redirect to the first match
                    mcq = possible_mcqs.first()
                    logger.warning(f"Legacy MCQ ID {mcq_id} redirected to MCQ {mcq.id}")
                    return redirect('view_mcq', mcq_id=mcq.id)
            except Exception as e:
                logger.error(f"Error handling legacy MCQ ID {mcq_id}: {e}")
        
        # If no match found, return 404
        raise Http404("MCQ not found")
    
    # Check if this MCQ is a flashcard for the current user
    is_flashcard = Flashcard.objects.filter(mcq=mcq, user=request.user).exists()
    
    # Check if this MCQ is bookmarked by the current user
    is_bookmarked = Bookmark.objects.filter(mcq=mcq, user=request.user).exists()
    
    # Check if this MCQ is hidden by the current user
    is_hidden = False
    if HiddenMCQ is not None:
        is_hidden = HiddenMCQ.objects.filter(mcq=mcq, user=request.user).exists()
    
    # Get user note for this MCQ
    try:
        user_note = Note.objects.get(mcq=mcq, user=request.user)
    except Note.DoesNotExist:
        user_note = None
    
    # Get next and previous MCQs in same subspecialty
    # Get all MCQs in the same subspecialty
    subspecialty_mcqs = MCQ.objects.filter(subspecialty=mcq.subspecialty).order_by('id')
    
    if not subspecialty_mcqs:
        next_mcq = None
        prev_mcq = None
    else:
        # Get the current MCQ's position
        current_mcq_ids = list(subspecialty_mcqs.values_list('id', flat=True))
        
        try:
            current_position = current_mcq_ids.index(mcq.id)
        except ValueError:
            # MCQ not found in list (shouldn't happen, but just in case)
            current_position = -1
        
        # Get next MCQ (with wrap-around)
        if current_position >= 0 and current_position < len(current_mcq_ids) - 1:
            next_mcq_id = current_mcq_ids[current_position + 1]
            next_mcq = MCQ.objects.get(id=next_mcq_id)
        elif current_position == len(current_mcq_ids) - 1:
            # Wrap to first
            next_mcq_id = current_mcq_ids[0]
            next_mcq = MCQ.objects.get(id=next_mcq_id)
        else:
            next_mcq = None
        
        # Get previous MCQ (with wrap-around)
        if current_position > 0:
            prev_mcq_id = current_mcq_ids[current_position - 1]
            prev_mcq = MCQ.objects.get(id=prev_mcq_id)
        elif current_position == 0:
            # Wrap to last
            prev_mcq_id = current_mcq_ids[-1]
            prev_mcq = MCQ.objects.get(id=prev_mcq_id)
        else:
            prev_mcq = None
            
    # Debug info
    print(f"Current MCQ: {mcq.id}, Next MCQ: {next_mcq.id if next_mcq else None}, Prev MCQ: {prev_mcq.id if prev_mcq else None}")
    
    MCQService.ensure_options_decoded(mcq)
    explanation_ctx = MCQService.build_explanation_context(mcq)

    initial_explanation_text = ""
    try:
        initial_explanation_text = mcq.get_unified_explanation_text()
    except AttributeError:
        initial_explanation_text = getattr(mcq, "unified_explanation", "") or getattr(mcq, "explanation", "") or ""

    # Normalize MCQ options for template rendering (support dict and list formats)
    option_pairs = []
    raw_options = getattr(mcq, 'options', None)
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    if isinstance(raw_options, dict):
        for idx, (key, value) in enumerate(raw_options.items()):
            letter = str(key).strip() or (letters[idx] if idx < len(letters) else f"Option {idx + 1}")
            option_pairs.append((letter, value))
    elif isinstance(raw_options, list):
        for idx, value in enumerate(raw_options):
            letter = letters[idx] if idx < len(letters) else f"Option {idx + 1}"
            option_pairs.append((letter, value))

    context = {
        'mcq': mcq,
        'user_note': user_note,
        'is_bookmarked': is_bookmarked,
        'is_flashcard': is_flashcard,
        'next_mcq': next_mcq,
        'prev_mcq': prev_mcq,
        'clean_explanation': explanation_ctx.clean_html,
        'has_proper_explanation': explanation_ctx.has_full_explanation,
        'has_structured_explanation': explanation_ctx.has_structured,
        'initial_explanation_text': initial_explanation_text,
        'SUBSPECIALTIES': SUBSPECIALTIES,  # Add the subspecialties list to the context
        'is_hidden': is_hidden,  # Add is_hidden to indicate if the MCQ is hidden for this user
        'option_pairs': option_pairs,
    }
    
    return render(request, 'mcq/mcq_detail.html', context)

@login_required
def test_image_display(request, mcq_id):
    """Test view to debug image display issues"""
    mcq = get_object_or_404(MCQ, id=mcq_id)
    return render(request, 'mcq/image_test.html', {'mcq': mcq})

@login_required
def check_answer(request, mcq_id):
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST requests allowed'})
    
    mcq = get_object_or_404(MCQ, id=mcq_id)
    selected_answer = request.POST.get('answer')
    is_correct = selected_answer == mcq.correct_answer
    
    # If the answer is incorrect, store it in the database for "Test My Weakness" feature
    if not is_correct and selected_answer:
        # Import the IncorrectAnswer model
        from .models import IncorrectAnswer
        
        # Check if we have any previous correct answers that should be marked as resolved
        previous_incorrects = IncorrectAnswer.objects.filter(
            user=request.user,
            mcq=mcq,
            resolved=False
        )
        
        # Create a new incorrect answer record
        IncorrectAnswer.objects.create(
            user=request.user,
            mcq=mcq,
            selected_answer=selected_answer
        )
        
        # Log this for debugging
        logger.info(f"User {request.user.username} answered MCQ {mcq_id} incorrectly with {selected_answer}. Stored for Test My Weakness feature.")
    # If answer is correct, mark any previous incorrect answers as resolved
    elif is_correct:
        from .models import IncorrectAnswer
        
        updated = IncorrectAnswer.objects.filter(
            user=request.user,
            mcq=mcq,
            resolved=False
        ).update(resolved=True)
        
        if updated:
            logger.info(f"User {request.user.username} answered MCQ {mcq_id} correctly. Marked {updated} previous incorrect answers as resolved.")
    
    return JsonResponse({
        'is_correct': is_correct,
        'correct_answer': mcq.correct_answer
    })

@login_required
def review_flashcards(request):
    # Get review type from query params
    review_type = request.GET.get('type', 'today')
    hidden_mcqs = MCQService.get_hidden_mcq_ids(request.user)
    due_flashcards = FlashcardService.due_flashcards_or_redirect(
        request, review_type, hidden_mcqs
    )
    if due_flashcards is None:
        return redirect('dashboard')

    flashcard = due_flashcards.first()
    mcq = flashcard.mcq if flashcard else None
    if mcq is None:
        messages.info(request, "No flashcards available for review.")
        return redirect('dashboard')

    context = {
        'mcq': mcq,
        'flashcard': flashcard,
        'flashcards_count': due_flashcards.count(),
        'review_type': review_type
    }
    
    return render(request, 'mcq/flashcard_review.html', context)

@login_required
def review_bookmarked(request):
    # Check if we're filtering by subspecialty
    subspecialty = request.GET.get('subspecialty', None)
    hidden_mcqs = MCQService.get_hidden_mcq_ids(request.user)
    bookmarked_query = BookmarkService.queryset_for_user(
        request.user, hidden_mcq_ids=hidden_mcqs
    )

    if subspecialty:
        bookmarked_query = bookmarked_query.filter(mcq__subspecialty=subspecialty)

    if not bookmarked_query.exists():
        message = (
            f"You don't have any bookmarked MCQs in the {subspecialty} subspecialty!"
            if subspecialty
            else "You don't have any bookmarked MCQs!"
        )
        messages.info(request, message)
        return redirect('dashboard')

    bookmark = bookmarked_query.first()
    if not bookmark:
        messages.info(request, "You don't have any bookmarked MCQs!")
        return redirect('dashboard')

    mcq = bookmark.mcq

    context = {
        'mcq': mcq,
        'bookmark': bookmark,
        'bookmarks_count': bookmarked_query.count(),
        'current_subspecialty': subspecialty
    }
    
    return render(request, 'mcq/bookmarked.html', context)

@login_required
@require_POST
def toggle_bookmark(request, mcq_id):
    mcq = get_object_or_404(MCQ, id=mcq_id)
    BookmarkService.toggle_with_feedback(request, mcq)
    return redirect('view_mcq', mcq_id=mcq_id)

@login_required
@require_POST
def save_note(request, mcq_id):
    mcq = get_object_or_404(MCQ, id=mcq_id)
    note_text = request.POST.get('note', '')
    NoteService.save_with_feedback(request, mcq, note_text)
    return redirect('view_mcq', mcq_id=mcq_id)

@login_required
@require_POST
def create_flashcard(request, mcq_id):
    mcq = get_object_or_404(MCQ, id=mcq_id)
    interval_raw = request.POST.get('interval', '1')
    try:
        interval = max(1, int(interval_raw))
    except (TypeError, ValueError):
        messages.error(request, "Invalid interval specified for flashcard creation.")
        return redirect('view_mcq', mcq_id=mcq_id)

    FlashcardService.schedule_with_feedback(request, mcq, interval)
    return redirect('view_mcq', mcq_id=mcq_id)

@login_required
@require_POST
def reclassify_mcq(request, mcq_id):
    mcq = get_object_or_404(MCQ, id=mcq_id)
    new_subspecialty = request.POST.get('subspecialty', '')
    
    if new_subspecialty:
        # Get database name if needed
        db_subspecialty = SUBSPECIALTY_MAPPING.get(new_subspecialty, new_subspecialty)
        
        # Update subspecialty
        mcq.subspecialty = db_subspecialty
        mcq.save()
        
        messages.success(request, f"MCQ #{mcq_id} reclassified to {new_subspecialty}")
    
    return redirect('view_mcq', mcq_id=mcq_id)

@login_required
@require_POST
def create_explanation(request, mcq_id):
    # Get the MCQ object using the ID from the URL
    mcq = get_object_or_404(MCQ, id=mcq_id)
    
    # Check if regeneration request includes a reason
    reason = request.POST.get('reason', '')
    
    # Setup logging
    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"Explanation generation request for MCQ #{mcq_id}")
    
    # Check if OpenAI integration is available
    from .openai_integration import api_key, client
    if not api_key or not client:
        logger.warning(f"OpenAI API not configured for MCQ #{mcq_id} explanation")
        error_html = """
        <div class="explanation-wrapper">
            <div class="alert alert-warning">
                <h4><i class="bi bi-exclamation-triangle"></i> OpenAI API Not Configured</h4>
                <p>AI features are not available because the OpenAI API key is not configured.</p>
                <p>Please contact the administrator to set up the OPENAI_API_KEY environment variable.</p>
            </div>
        </div>
        """
        mcq.explanation = error_html
        mcq.save()
        
        return JsonResponse({
            'success': False,
            'message': 'OpenAI API key not configured. AI features are disabled.'
        })
    
    try:
        # Generate the explanation using OpenAI with a timeout
        from threading import Thread
        import time
        import uuid
        
        # Create a placeholder explanation with generating message
        placeholder_id = str(uuid.uuid4())
        progress_html = f"""
        <div class="explanation-wrapper" id="explanation-{placeholder_id}">
            <div class="alert alert-info">
                <h4><i class="bi bi-hourglass-split"></i> Generating explanation...</h4>
                <p>This may take up to 2 minutes to complete. The page will automatically update when ready.</p>
                <div class="progress mt-3">
                    <div class="progress-bar progress-bar-striped progress-bar-animated" role="progressbar" 
                        style="width: 100%" aria-valuenow="100" aria-valuemin="0" aria-valuemax="100"></div>
                </div>
            </div>
            
            <script>
                // Poll for explanation completion
                (function checkExplanation() {{
                    fetch('/mcq/{mcq_id}/check_explanation/?id={placeholder_id}')
                    .then(response => response.json())
                    .then(data => {{
                        if (data.ready) {{
                            // Replace placeholder with real explanation
                            document.getElementById('explanation-{placeholder_id}').innerHTML = data.explanation;
                        }} else {{
                            // Check again after 5 seconds
                            setTimeout(checkExplanation, 5000);
                        }}
                    }})
                    .catch(error => {{
                        console.error('Error checking explanation:', error);
                        // Check again after 5 seconds even if there's an error
                        setTimeout(checkExplanation, 5000);
                    }});
                }})();
            </script>
        </div>
        """
        
        # Save the placeholder first
        mcq.explanation = progress_html
        mcq.save()
        
        # Start a background thread to generate the explanation
        def generate_explanation_thread():
            try:
                # CRITICAL: Create a reference to the MCQ to prevent scope issues
                # Get a fresh instance of the MCQ to ensure we have the latest data
                # Don't use closed-over mcq variable which might be stale
                current_mcq = get_object_or_404(MCQ, id=mcq_id)
                
                logger.info(f"Background thread starting explanation generation for MCQ #{mcq_id}")
                
                # Generate explanation
                explanation = generate_explanation(current_mcq, reason)
                
                logger.info(f"Generated raw explanation for MCQ #{mcq_id}, length: {len(explanation)}")
                
                # Process explanation using the existing processing code with the current MCQ reference
                processed_explanation = _process_explanation(explanation, current_mcq)
                
                logger.info(f"Processed explanation for MCQ #{mcq_id}, length: {len(processed_explanation)}")
                
                # Update the MCQ with the real explanation
                current_mcq.explanation = processed_explanation
                current_mcq.save()
                
                logger.info(f"Successfully saved explanation for MCQ #{mcq_id}")
            except Exception as e:
                # Log the error for debugging
                logger.error(f"Error generating explanation for MCQ #{mcq_id}: {str(e)}", exc_info=True)
                
                # If there's an error, save it as the explanation
                error_html = f"""
                <div class="explanation-wrapper">
                    <div class="alert alert-danger">
                        <h4><i class="bi bi-exclamation-triangle"></i> Error generating explanation</h4>
                        <p>We encountered an error while generating the explanation: {str(e)}</p>
                        <p>Please try again later.</p>
                    </div>
                </div>
                """
                
                # Get a fresh instance of the MCQ to prevent conflict
                try:
                    current_mcq = get_object_or_404(MCQ, id=mcq_id)
                    current_mcq.explanation = error_html
                    current_mcq.save()
                    logger.info(f"Saved error message for failed explanation of MCQ #{mcq_id}")
                except Exception as inner_e:
                    logger.error(f"Error saving error message to MCQ #{mcq_id}: {str(inner_e)}", exc_info=True)
        
        # Enqueue Celery task instead of local thread to avoid web dyno time limits
        try:
            from .tasks import run_ai_job
            import uuid as _uuid
            job_id = str(_uuid.uuid4())
            cache.set(f"ai_job:{job_id}", {'status': 'queued'}, timeout=3600)
            run_ai_job.delay(job_id, 'generate_explanation', {
                'mcq_id': mcq_id,
                'reason': reason,
                'user_id': request.user.id,
            })
            logger.info(f"Queued explanation generation job {job_id} for MCQ #{mcq_id}")
        except Exception as e:
            logger.error(f"Failed to enqueue explanation generation job: {e}")
        
        # Return a success message immediately
        return JsonResponse({
            'success': True,
            'message': 'Explanation generation started. Please wait while we generate your explanation.',
            'placeholder_id': placeholder_id
        })
        
    except Exception as e:
        logger.error(f"Error starting explanation generation for MCQ #{mcq_id}: {str(e)}", exc_info=True)
        return JsonResponse({
            'success': False,
            'message': f'Error starting explanation generation: {str(e)}'
        })

@login_required
def check_explanation(request, mcq_id):
    mcq = get_object_or_404(MCQ, id=mcq_id)
    placeholder_id = request.GET.get('id', '')
    
    # Check if the explanation is still a placeholder
    if mcq.explanation and f'id="explanation-{placeholder_id}"' in mcq.explanation and 'Generating explanation' in mcq.explanation:
        return JsonResponse({
            'ready': False
        })
    
    # Explanation is ready
    return JsonResponse({
        'ready': True,
        'explanation': mcq.explanation
    })

# Process the explanation text into HTML
def _process_explanation(explanation, current_mcq=None):
    import re
    import logging
    
    logger = logging.getLogger(__name__)
    
    # Check if current_mcq is None and log it
    if current_mcq is None:
        logger.warning("_process_explanation called with current_mcq=None. Some formatting features will be limited.")
    
    # Create TOC and wrapper for explanation
    toc_html = '<div class="toc-container mb-4"><h4><i class="bi bi-list-ul"></i> Contents</h4><ul class="toc-list">'
    explanation_html = '<div class="explanation-wrapper">'
    
    # Section Headers with icons (and add to TOC)
    sections = [
        (r'# Explanation of MCQ: (.*?)\n', 'explanation-title', '<i class="bi bi-book"></i> Explanation of MCQ: $1', 'Overview'),
        (r'# Question Recap\n', 'question-recap', '<i class="bi bi-chat-quote"></i> Question Recap', 'Question Recap'),
        (r'# Why the Correct Answer is Right\n', 'correct-answer', '<i class="bi bi-check-circle"></i> Why the Correct Answer is Right', 'Correct Answer'),
        (r'# Why Each Wrong Answer is Wrong\n', 'wrong-answers', '<i class="bi bi-x-circle"></i> Why Each Wrong Answer is Wrong', 'Wrong Answers'),
        (r'# Comparison Table of Options\n', 'comparison-table', '<i class="bi bi-table"></i> Comparison Table of Options', 'Comparison Table'),
        (r'# Clinical Pearls to Memorize\n', 'clinical-pearls', '<i class="bi bi-lightbulb"></i> Clinical Pearls to Memorize', 'Clinical Pearls'),
        (r'# Summary of Latest Guidelines\n', 'guidelines', '<i class="bi bi-journal-medical"></i> Summary of Latest Guidelines', 'Guidelines'),
        (r'# Historical Note\n', 'historical-note', '<i class="bi bi-clock-history"></i> Historical Note', 'Historical Note')
    ]
    
    for i, (pattern, section_id, header_html, toc_label) in enumerate(sections):
        toc_html += f'<li><a href="#section-{section_id}" class="toc-link">{toc_label}</a></li>'
        
        # For the first section (title)
        if i == 0:
            m = re.search(pattern, explanation)
            if m:
                title = m.group(1)
                explanation = re.sub(pattern, 
                    f'<section id="section-{section_id}" class="explanation-section">'
                    f'<div class="section-header" data-bs-toggle="collapse" data-bs-target="#content-{section_id}" aria-expanded="true">'
                    f'<h1>{header_html}</h1>'
                    f'<i class="toggle-icon bi bi-chevron-up"></i>'
                    f'</div>'
                    f'<div class="section-content collapse show" id="content-{section_id}">', explanation)
            else:
                # If no match, add the standard header
                explanation_html += (
                    f'<section id="section-{section_id}" class="explanation-section">'
                    f'<div class="section-header" data-bs-toggle="collapse" data-bs-target="#content-{section_id}" aria-expanded="true">'
                    f'<h1>{header_html.replace("$1", "")}</h1>'
                    f'<i class="toggle-icon bi bi-chevron-up"></i>'
                    f'</div>'
                    f'<div class="section-content collapse show" id="content-{section_id}">'
                )
                
        # For all other sections
        else:
            # Close previous section if needed
            if i > 0:
                explanation = re.sub(pattern, 
                    f'</div></section>'
                    f'<section id="section-{section_id}" class="explanation-section {section_id}">'
                    f'<div class="section-header" data-bs-toggle="collapse" data-bs-target="#content-{section_id}" aria-expanded="true">'
                    f'<h2>{header_html}</h2>'
                    f'<i class="toggle-icon bi bi-chevron-up"></i>'
                    f'</div>'
                    f'<div class="section-content collapse show" id="content-{section_id}">', explanation)
    
    # Close the last section and complete the TOC
    explanation += '</div></section>'
    toc_html += '</ul></div>'
    
    # Subsection Headers
    explanation = re.sub(r'## Option ([A-D]): (.*?)\n', 
                         r'<h3 class="subsection-header option-header"><span class="option-badge option-\1">\1</span> \2</h3>', 
                         explanation)
    explanation = re.sub(r'## (.*?)\n', 
                         r'<h3 class="subsection-header"><i class="bi bi-arrow-right-circle"></i> \1</h3>', 
                         explanation)
    
    # Bold and italic formatting
    explanation = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', explanation)
    explanation = re.sub(r'\*(.*?)\*', r'<em>\1</em>', explanation)
    
    # Lists with icons
    explanation = re.sub(r'- (.*?)\n', r'<li class="bullet-item"><i class="bi bi-dash"></i> \1</li>', explanation)
    explanation = re.sub(r'(\d+)\. (.*?)\n', 
                         r'<li class="numbered-item"><span class="number-badge">\1</span> \2</li>', 
                         explanation)
    
    # Enhanced table handling with better pattern matching and formatting
    table_pattern = r'\|[ \t]*(.*?)[ \t]*\|[ \t]*\n\|[ \t]*([-|\s:]*?)[ \t]*\|[ \t]*\n((?:\|.*?\|[ \t]*\n)+)'
    
    def table_replace(match):
        # Process the table header row
        nonlocal current_mcq  # Use the parent scope's current_mcq variable
        header_str = match.group(1)
        headers = [h.strip() for h in header_str.split('|') if h.strip()]
        
        # Get all table rows
        rows_text = match.group(3)
        rows = []
        for row_text in rows_text.strip().split('\n'):
            # Remove leading/trailing pipes and split by pipe
            row_text = row_text.strip()
            if row_text.startswith('|'):
                row_text = row_text[1:]
            if row_text.endswith('|'):
                row_text = row_text[:-1]
            
            cells = [cell.strip() for cell in row_text.split('|')]
            rows.append(cells)
        
        # Ensure consistent cell count
        max_cells = max(len(headers), *[len(row) for row in rows])
        for row in rows:
            while len(row) < max_cells:
                row.append("")
        
        # Create HTML table with Bootstrap styling and card container
        html = '<div class="table-card card shadow-sm mb-4"><div class="table-responsive">'
        html += '<table class="table table-striped table-hover table-bordered mb-0">'
        
        # Table header with better formatting
        html += '<thead class="table-primary"><tr>'
        for h in headers:
            html += f'<th class="text-center">{h}</th>'
        # Add any missing columns in header
        for _ in range(max_cells - len(headers)):
            html += f'<th class="text-center"></th>'
        html += '</tr></thead>'
        
        # Table body with enhanced styling
        html += '<tbody>'
        for i, row in enumerate(rows):
            row_class = 'table-light' if i % 2 == 0 else ''
            html += f'<tr class="{row_class}">'
            
            for j, cell in enumerate(row):
                cell_content = cell.strip()
                
                # Enhanced cell formatting based on content
                if cell_content in ['A', 'B', 'C', 'D', 'E']:
                    # Option badge format
                    html += f'<td class="text-center"><span class="option-badge option-{cell_content}">{cell_content}</span></td>'
                
                elif any(marker in cell_content.lower() for marker in ['correct', 'yes', 'true', 'recommended', '✓', '✔']):
                    # Success indicators
                    html += f'<td class="text-center"><span class="badge bg-success">{cell_content}</span></td>'
                
                elif any(marker in cell_content.lower() for marker in ['incorrect', 'no', 'false', 'avoid', 'contraindicated', '✗', '✘']):
                    # Error/negative indicators
                    html += f'<td class="text-center"><span class="badge bg-danger">{cell_content}</span></td>'
                
                elif any(level in cell_content for level in ['Level A', 'Class I', 'Grade A']):
                    # Strong evidence formatting
                    html += f'<td><span class="badge bg-success me-2">Strong Evidence</span>{cell_content}</td>'
                
                elif any(level in cell_content for level in ['Level B', 'Class II', 'Grade B']):
                    # Moderate evidence formatting
                    html += f'<td><span class="badge bg-primary me-2">Moderate Evidence</span>{cell_content}</td>'
                
                elif any(level in cell_content for level in ['Level C', 'Class III', 'Grade C']):
                    # Weak evidence formatting
                    html += f'<td><span class="badge bg-info me-2">Limited Evidence</span>{cell_content}</td>'
                
                elif any(level in cell_content for level in ['Expert Opinion', 'Consensus', 'Good Practice']):
                    # Expert opinion formatting
                    html += f'<td><span class="badge bg-secondary me-2">Expert Opinion</span>{cell_content}</td>'
                
                else:
                    # Apply special formatting for the correct option row
                    # FIXED: Check that current_mcq is not None and has correct_answer before using it
                    correct_answer = None
                    if current_mcq is not None and hasattr(current_mcq, 'correct_answer'):
                        correct_answer = current_mcq.correct_answer
                    
                    if correct_answer and j == 0 and cell_content == correct_answer:
                        html += f'<td><strong class="text-success">{cell_content}</strong> (Correct)</td>'
                    else:
                        # Default formatting with proper handling of bolded text
                        cell_content = cell_content.replace('**', '<strong>').replace('**', '</strong>')
                        html += f'<td>{cell_content}</td>'
            
            html += '</tr>'
        html += '</tbody></table></div></div>'
        
        return html
    
    explanation = re.sub(table_pattern, table_replace, explanation)
    
    # Fix unordered lists
    explanation = re.sub(r'(<li class="bullet-item">.*?</li>)+', r'<ul class="custom-ul">\g<0></ul>', explanation)
    
    # Fix ordered lists
    explanation = re.sub(r'(<li class="numbered-item">.*?</li>)+', r'<ol class="custom-ol">\g<0></ol>', explanation)
    
    # Special formatting for key sections
    explanation = re.sub(r'Level of evidence:\s*([A-D])', 
                        r'<div class="evidence-level card p-2 mb-3">'
                        r'<div class="evidence-header"><i class="bi bi-award"></i> Evidence Level:</div>'
                        r'<div class="evidence-content"><span class="badge bg-primary evidence-badge">\1</span></div>'
                        r'</div>', 
                        explanation)
    
    explanation = re.sub(r'Key recommendation:\s*(.*?)(?=<)', 
                        r'<div class="key-recommendation card p-2 mb-3">'
                        r'<div class="recommendation-header"><i class="bi bi-star"></i> Key Recommendation:</div>'
                        r'<div class="recommendation-content">\1</div>'
                        r'</div>',
                        explanation)
    
    # References formatting
    explanation = re.sub(r'References?:?\n((?:(?:\d+\.|\-)[^\n]+\n)+)', 
                      r'<div class="references-section p-3 mb-3 mt-3 border-top">'
                      r'<h4 class="references-header mb-2"><i class="bi bi-journal-text"></i> References</h4>'
                      r'<ol class="references-list">\1</ol></div>', 
                      explanation)
    
    # Convert reference list items
    explanation = re.sub(r'(?<=<ol class="references-list">)(\d+\.\s*)(.*?)(?=\n\d+\.|\n<\/ol>)', 
                         r'<li class="reference-item">\2</li>', 
                         explanation)
    
    # Add print button
    print_button = """
    <div class="mb-3 d-flex justify-content-end">
        <button class="btn btn-sm btn-outline-secondary print-explanation" 
            onclick="window.print()">
            <i class="bi bi-printer"></i> Print Explanation
        </button>
    </div>
    """
    
    # Enhanced CSS for explanation with improved formatting and consistency
    css = """
    <style>
    /* Print styles */
    @media print {
        body * {
            visibility: hidden;
        }
        .explanation-wrapper, .explanation-wrapper * {
            visibility: visible;
        }
        .explanation-wrapper {
            position: absolute;
            left: 0;
            top: 0;
            width: 100%;
        }
        .toggle-icon, .print-explanation, .toc-container {
            display: none !important;
        }
        .section-content {
            display: block !important;
        }
    }
    
    /* Table of Contents */
    .toc-container {
        background-color: #f8f9fa;
        border-radius: 5px;
        padding: 15px;
        border-left: 5px solid #6c757d;
        margin-bottom: 20px;
        box-shadow: 0 2px 4px rgba(0,0,0,0.05);
    }
    .toc-list {
        list-style-type: none;
        padding-left: 10px;
        margin-bottom: 0;
    }
    .toc-link {
        display: block;
        padding: 8px 0;
        color: #495057;
        text-decoration: none;
        transition: all 0.2s;
        font-weight: 500;
    }
    .toc-link:hover {
        color: #0d6efd;
        transform: translateX(5px);
    }
    
    /* Section styling */
    .explanation-section {
        margin-bottom: 30px;
        border-radius: 8px;
        overflow: hidden;
        box-shadow: 0 2px 5px rgba(0,0,0,0.1);
    }
    .section-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 15px;
        background-color: #f8f9fa;
        cursor: pointer;
        transition: all 0.3s;
    }
    .section-header:hover {
        background-color: #e9ecef;
    }
    .section-header h1, .section-header h2 {
        margin: 0;
        font-size: 1.5rem;
        color: #343a40;
        font-weight: 600;
        line-height: 1.4;
    }
    .toggle-icon {
        font-size: 1.2rem;
        transition: transform 0.3s;
    }
    .section-header[aria-expanded="false"] .toggle-icon {
        transform: rotate(180deg);
    }
    .section-content {
        padding: 25px;
        background-color: #ffffff;
        line-height: 1.6;
    }
    
    /* Specific section colors */
    .explanation-section.correct-answer .section-header {
        background-color: rgba(25, 135, 84, 0.1);
        border-left: 5px solid #198754;
    }
    .explanation-section.wrong-answers .section-header {
        background-color: rgba(220, 53, 69, 0.1);
        border-left: 5px solid #dc3545;
    }
    .explanation-section.clinical-pearls .section-header {
        background-color: rgba(255, 193, 7, 0.1);
        border-left: 5px solid #ffc107;
    }
    .explanation-section.guidelines .section-header {
        background-color: rgba(13, 110, 253, 0.1);
        border-left: 5px solid #0d6efd;
    }
    .explanation-section.comparison-table .section-header {
        background-color: rgba(108, 117, 125, 0.1);
        border-left: 5px solid #6c757d;
    }
    .explanation-section.historical-note .section-header {
        background-color: rgba(108, 117, 125, 0.1);
        border-left: 5px solid #6c757d;
    }
    
    /* Subsection headers */
    .subsection-header {
        margin-top: 24px;
        margin-bottom: 16px;
        color: #343a40;
        padding-bottom: 10px;
        border-bottom: 1px solid #dee2e6;
        font-weight: 600;
        line-height: 1.4;
    }
    .option-header {
        display: flex;
        align-items: center;
    }
    
    /* Option badges */
    .option-badge {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 32px;
        height: 32px;
        border-radius: 50%;
        margin-right: 12px;
        font-weight: bold;
        box-shadow: 0 2px 4px rgba(0,0,0,0.1);
    }
    .option-A { background-color: #0d6efd; color: white; }
    .option-B { background-color: #198754; color: white; }
    .option-C { background-color: #ffc107; color: black; }
    .option-D { background-color: #dc3545; color: white; }
    .option-E { background-color: #6c757d; color: white; }
    
    /* Number badges */
    .number-badge {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 28px;
        height: 28px;
        border-radius: 50%;
        background-color: #0d6efd;
        color: white;
        margin-right: 12px;
        font-weight: bold;
        box-shadow: 0 2px 4px rgba(0,0,0,0.1);
    }
    
    /* List styling */
    .custom-ul, .custom-ol {
        padding-left: 0;
        list-style-type: none;
        margin-bottom: 1.5rem;
    }
    .bullet-item, .numbered-item {
        display: flex;
        align-items: flex-start;
        margin-bottom: 12px;
        padding: 8px;
        border-radius: 5px;
        transition: background-color 0.2s;
        line-height: 1.6;
    }
    .bullet-item:hover, .numbered-item:hover {
        background-color: rgba(13, 110, 253, 0.05);
    }
    .bullet-item i, .numbered-item .number-badge {
        margin-top: 2px;
        flex-shrink: 0;
    }
    
    /* Evidence and recommendations */
    .evidence-level, .key-recommendation {
        background-color: #f8f9fa;
        border-radius: 5px;
        padding: 15px;
        margin-bottom: 20px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.05);
    }
    .evidence-header, .recommendation-header {
        font-weight: 600;
        margin-bottom: 8px;
        color: #343a40;
    }
    .evidence-badge {
        font-size: 1rem;
        padding: 5px 10px;
    }
    
    /* Table card */
    .table-card {
        border: none;
        overflow: hidden;
        margin: 25px 0;
        box-shadow: 0 2px 5px rgba(0,0,0,0.08);
    }
    .table {
        margin-bottom: 0;
    }
    .table th {
        font-weight: 600;
        vertical-align: middle;
        background-color: #f1f8ff;
        text-align: center;
        padding: 12px;
    }
    .table td {
        vertical-align: middle;
        padding: 10px 12px;
        line-height: 1.5;
    }
    
    /* References section */
    .references-section {
        background-color: #f9f9f9;
        border-radius: 5px;
        border-left: 3px solid #6c757d;
    }
    .references-header {
        color: #343a40;
        font-size: 1.1rem;
        font-weight: 600;
        margin-bottom: 12px;
    }
    .references-list {
        padding-left: 20px;
        margin-bottom: 0;
    }
    .reference-item {
        margin-bottom: 8px;
        line-height: 1.5;
    }
    
    /* Misc */
    strong {
        color: #343a40;
        font-weight: 600;
    }
    em {
        font-style: italic;
    }
    p {
        margin-bottom: 1rem;
        line-height: 1.6;
    }
    </style>
    """
    
    # Add JavaScript for collapsible sections
    javascript = """
    <script>
    document.addEventListener('DOMContentLoaded', function() {
        // Initialize collapsible sections
        document.querySelectorAll('.section-header').forEach(function(header) {
            header.addEventListener('click', function() {
                const target = this.getAttribute('data-bs-target');
                const content = document.querySelector(target);
                
                // Toggle the collapse state
                if (content.classList.contains('show')) {
                    content.classList.remove('show');
                    this.setAttribute('aria-expanded', 'false');
                } else {
                    content.classList.add('show');
                    this.setAttribute('aria-expanded', 'true');
                }
            });
        });
        
        // Smooth scrolling for TOC links
        document.querySelectorAll('.toc-link').forEach(function(link) {
            link.addEventListener('click', function(e) {
                e.preventDefault();
                const targetId = this.getAttribute('href');
                const targetElement = document.querySelector(targetId);
                
                if (targetElement) {
                    // Scroll to the element
                    targetElement.scrollIntoView({
                        behavior: 'smooth',
                        block: 'start'
                    });
                    
                    // Ensure the section is expanded
                    const contentId = targetId.replace('section-', 'content-');
                    const contentElement = document.querySelector('#' + contentId);
                    if (contentElement && !contentElement.classList.contains('show')) {
                        contentElement.classList.add('show');
                        const header = targetElement.querySelector('.section-header');
                        if (header) {
                            header.setAttribute('aria-expanded', 'true');
                        }
                    }
                }
            });
        });
    });
    </script>
    """
    
    # Combine everything
    final_explanation = css + print_button + toc_html + explanation_html + explanation + '</div>' + javascript
    
    return final_explanation

@login_required
@require_POST
def improve_mcq_view(request, mcq_id):
    mcq = get_object_or_404(MCQ, id=mcq_id)
    
    # Check if OpenAI integration is available
    from .openai_integration import api_key, client
    if not api_key or not client:
        messages.warning(request, "AI features are not available. Please contact the administrator to set up the OpenAI API.")
        return redirect('view_mcq', mcq_id=mcq_id)
    
    # Store original question for comparison
    original_question = mcq.question_text
    
    # Generate improved question (only rephrased for clarity) using OpenAI
    improved_question = improve_question(mcq)
    
    # Check if the question actually changed
    if improved_question == original_question:
        messages.info(request, f"No changes were made to MCQ #{mcq_id}. The question already has optimal phrasing.")
        return redirect('view_mcq', mcq_id=mcq_id)
    
    # Update the database with the improved question
    mcq.question_text = improved_question
    mcq.save()
    
    messages.success(request, f"MCQ #{mcq_id} rephrased for clarity. No medical content was altered.")
    return redirect('view_mcq', mcq_id=mcq_id)

@login_required
@require_POST
def new_options_view(request, mcq_id):
    mcq = get_object_or_404(MCQ, id=mcq_id)
    
    # Check if OpenAI integration is available
    from .openai_integration import api_key, client
    if not api_key or not client:
        messages.warning(request, "AI features are not available. Please contact the administrator to set up the OpenAI API.")
        return redirect('view_mcq', mcq_id=mcq_id)
    
    try:
        # Log operation start
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"Generating new options for MCQ #{mcq_id}")
        
        # Generate new options using OpenAI
        new_options_result = generate_new_options(mcq)
        
        # Log the result type for debugging
        logger.info(f"New options result type: {type(new_options_result)}, value: {new_options_result}")
        
        # Handle different response formats
        if isinstance(new_options_result, dict):
            if 'error' in new_options_result:
                # Handle error in result
                error_msg = new_options_result.get('error', 'Unknown error generating options')
                logger.error(f"Error in OpenAI response: {error_msg}")
                messages.error(request, f"Error: {error_msg}")
                return redirect('view_mcq', mcq_id=mcq_id)
                
            # Valid dictionary response - update the MCQ
            logger.info(f"Valid options generated: {new_options_result}")
            mcq.options = new_options_result
            mcq.save()
            messages.success(request, f"New options generated for MCQ #{mcq_id}")
        elif isinstance(new_options_result, str):
            # Try to parse the string as JSON
            try:
                import json
                options_dict = json.loads(new_options_result)
                if isinstance(options_dict, dict):
                    logger.info(f"Parsed string response as JSON: {options_dict}")
                    mcq.options = options_dict
                    mcq.save()
                    messages.success(request, f"New options generated for MCQ #{mcq_id}")
                else:
                    logger.error(f"Parsed response is not a dict: {options_dict}")
                    messages.error(request, "Error: Generated options are not in the correct format")
            except json.JSONDecodeError:
                # Not valid JSON
                logger.error(f"Could not parse string response as JSON: {new_options_result}")
                messages.error(request, "Error: Generated options are not in valid JSON format")
        else:
            # Unknown response type
            logger.error(f"Unexpected response type: {type(new_options_result)}")
            messages.error(request, "Error: Unexpected response format from AI service")
    
    except Exception as e:
        # Handle any exceptions
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Exception generating new options for MCQ #{mcq_id}: {str(e)}")
        messages.error(request, f"Error: {str(e)}")
    
    return redirect('view_mcq', mcq_id=mcq_id)

# verify_answer view has been removed

@login_required
def ask_gpt(request, mcq_id):
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST requests allowed'})
    
    question = request.POST.get('question', '')
    
    if not question:
        return JsonResponse({'error': 'Question is required'})
    
    mcq = get_object_or_404(MCQ, id=mcq_id)
    
    # Check if OpenAI integration is available
    from .openai_integration import api_key, client
    if not api_key or not client:
        return JsonResponse({
            'answer': "<div class='alert alert-warning'><h4><i class='bi bi-exclamation-triangle'></i> AI Features Unavailable</h4><p>AI-Pal requires OpenAI API access, which is not currently configured.</p><p>Please contact the administrator to set up the OpenAI API key.</p></div>"
        })
    
    # Get answer from OpenAI
    answer = answer_question_about_mcq(mcq, question)
    
    return JsonResponse({
        'answer': answer
    })

@login_required
def ask_gpt_async(request, mcq_id):
    """Submit Ask GPT as a background job and return a job_id for polling."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST requests allowed'}, status=405)
    question = request.POST.get('question', '').strip()
    if not question:
        return JsonResponse({'error': 'Question is required'}, status=400)
    # Create job
    job_id = str(uuid.uuid4())
    cache.set(f"ai_job:{job_id}", {'status': 'pending'}, timeout=3600)
    # Enqueue task
    try:
        from .tasks import run_ai_job
        run_ai_job.delay(job_id, 'ask_gpt', {'mcq_id': mcq_id, 'question': question, 'user_id': request.user.id})
    except Exception as e:
        return JsonResponse({'error': f'Failed to enqueue job: {str(e)}'}, status=500)
    return JsonResponse({'job_id': job_id, 'status': 'queued'})

@login_required
def ai_job_status(request, job_id):
    """Return status and result for a background AI job."""
    data = cache.get(f"ai_job:{job_id}")
    if not data:
        return JsonResponse({'error': 'Job not found'}, status=404)
    return JsonResponse(data)

@login_required
def diagnostics_view(request):
    # Check database status
    mcq_count = MCQ.objects.count()
    
    # Get subspecialties
    subspecialties = MCQ.objects.values('subspecialty').annotate(
        count=Count('id')).order_by('subspecialty')
    
    # Get first 5 MCQs
    sample_mcqs = MCQ.objects.all().order_by('id')[:5]
    
    context = {
        'mcq_count': mcq_count,
        'subspecialties': subspecialties,
        'sample_mcqs': sample_mcqs,
    }
    
    return render(request, 'mcq/diagnostics.html', context)

# Import form route
@login_required
def import_mcqs_form(request):
    # Restrict access to admin users only
    if not request.user.is_superuser:
        messages.error(request, "You don't have permission to access this page.")
        return redirect('dashboard')
    if request.method == 'POST':
        # Handle JSON file import
        if request.FILES.get('json_file'):
            json_file = request.FILES['json_file']
            
            try:
                mcqs_data = json.loads(json_file.read().decode('utf-8'))
                
                total_count = len(mcqs_data)
                imported_count = 0
                existing_count = 0
                
                for mcq_data in mcqs_data:
                    # Check if MCQ already exists
                    if MCQ.objects.filter(
                        question_number=mcq_data.get('question_number'),
                        question_text=mcq_data.get('question_text')
                    ).exists():
                        existing_count += 1
                        continue
                    
                    # Create the MCQ
                    MCQ.objects.create(
                        question_number=mcq_data.get('question_number'),
                        question_text=mcq_data.get('question_text', 'Missing question text'),
                        options=mcq_data.get('options'),
                        correct_answer=mcq_data.get('correct_answer', 'A'),
                        subspecialty=mcq_data.get('subspecialty', 'Other/Unclassified'),
                        source_file=mcq_data.get('source_file'),
                        exam_type=mcq_data.get('exam_type'),
                        exam_year=mcq_data.get('exam_year'),
                        explanation=mcq_data.get('explanation')
                    )
                    imported_count += 1
                
                messages.success(request, f'Successfully imported {imported_count} MCQs. {existing_count} were already in the database.')
                return redirect('dashboard')
                
            except Exception as e:
                messages.error(request, f'Error importing MCQs from JSON: {str(e)}')
        
        # Handle text format import
        elif request.POST.get('mcqs_text'):
            mcqs_text = request.POST.get('mcqs_text')
            
            try:
                # Split by the separator
                mcq_blocks = mcqs_text.split('--------------------------------------------------')
                
                total_count = len(mcq_blocks)
                imported_count = 0
                existing_count = 0
                
                for mcq_block in mcq_blocks:
                    if not mcq_block.strip():
                        continue
                    
                    # Parse the MCQ block
                    lines = mcq_block.strip().split('\n')
                    
                    # Extract question number and source - handle different formats
                    question_match = re.search(r'Q(\d+)\.?\s*(?:\(Source:\s*(.*?)\))?', lines[0])
                    if not question_match:
                        # Try alternative formats
                        alt_match = re.search(r'Q\.?\s*(\d+)|Question\s*(\d+)', lines[0], re.IGNORECASE)
                        if alt_match:
                            question_num = alt_match.group(1) or alt_match.group(2)
                            question_number = f"Q{question_num}"
                            # Look for source in the first few lines
                            source_file = ""
                            for i in range(min(3, len(lines))):
                                source_search = re.search(r'Source:\s*(.*?)(?:\s|$)', lines[i], re.IGNORECASE)
                                if source_search:
                                    source_file = source_search.group(1).strip()
                                    break
                        else:
                            # If we still can't find a question number, skip this block
                            continue
                    else:
                        question_number = f"Q{question_match.group(1)}"
                        source_file = question_match.group(2).strip() if question_match.group(2) else ""
                    
                    # Extract exam type and year from source
                    exam_type = None
                    exam_year = None
                    
                    # More comprehensive pattern matching for exam type
                    if re.search(r'Part\s*I\b|Part\s*1\b', source_file, re.IGNORECASE):
                        exam_type = "Part I"
                    elif re.search(r'Part\s*II\b|Part\s*2\b|part\s*2\b|part\s*II\b', source_file, re.IGNORECASE):
                        exam_type = "Part II"
                    elif re.search(r'Promotion', source_file, re.IGNORECASE):
                        exam_type = "Promotion"
                    elif re.search(r'ABPN', source_file, re.IGNORECASE):
                        exam_type = "ABPN Board"
                    
                    # Extract year with improved pattern
                    year_match = re.search(r'20\d{2}', source_file)
                    if year_match:
                        exam_year = int(year_match.group(0))
                    
                    # Extract question text with improved handling for different formats
                    question_text = ""
                    line_index = 1
                    
                    # Skip any empty lines at the beginning
                    while line_index < len(lines) and not lines[line_index].strip():
                        line_index += 1
                    
                    # Read until we hit something that looks like an option (A., A:, etc.)
                    while line_index < len(lines):
                        line = lines[line_index].strip()
                        # Stop if we hit an option line
                        if re.match(r'^[A-E][\.\)\-:\s]', line):
                            break
                        # Add non-empty lines to question text
                        if line:
                            question_text += line + " "
                        line_index += 1
                    
                    question_text = question_text.strip()
                    
                    # Extract options and correct answer with improved handling
                    options = {}
                    correct_answer = None
                    
                    while line_index < len(lines) and lines[line_index].strip() and not lines[line_index].strip().startswith('*'):
                        line = lines[line_index].strip()
                        
                        # Match various option formats: A., A), A-, etc.
                        option_match = re.match(r'^([A-E])[\.\)\-:\s]\s*(.*)', line)
                        
                        if option_match:
                            option_letter = option_match.group(1)
                            option_text = option_match.group(2).strip()
                            
                            # Check for [CORRECT] marker in different formats
                            if re.search(r'\[CORRECT\]|\(CORRECT\)|CORRECT', option_text, re.IGNORECASE):
                                correct_answer = option_letter
                                # Remove any correct markers with different bracket types
                                option_text = re.sub(r'\s*[\[\(]CORRECT[\]\)]|\s*CORRECT', '', option_text, flags=re.IGNORECASE).strip()
                            
                            options[option_letter] = option_text
                        elif line and line_index > 0 and options:
                            # This might be a continuation of the previous option
                            last_option = list(options.keys())[-1]
                            options[last_option] += " " + line
                        
                        line_index += 1
                    
                    # If no correct answer was found but there's a CORRECT ANSWER: line, check for that
                    if not correct_answer:
                        for line in lines:
                            correct_match = re.search(r'CORRECT\s+ANSWER\s*:\s*([A-E])', line, re.IGNORECASE)
                            if correct_match:
                                correct_answer = correct_match.group(1)
                                break
                    
                    # Extract explanation/classification with improved detection
                    explanation = ""
                    for line in lines:
                        # Match different formats of classification reason
                        if re.match(r'^\s*\*+\s*(Classification\s*Reason:|Explanation:)', line, re.IGNORECASE):
                            explanation = line.strip()
                            break
                    
                    # If no explanation found, look for any line starting with * that might be a comment
                    if not explanation:
                        for line in lines:
                            if line.strip().startswith('*') and len(line.strip()) > 2:
                                explanation = line.strip()
                                break
                    
                    # Determine subspecialty from classification reason
                    subspecialty = "Other/Unclassified"
                    
                    # Map of keywords to subspecialties
                    subspecialty_keywords = {
                        "dementia": "Dementia",
                        "alzheimer": "Dementia",
                        "frontotemporal": "Dementia",
                        "memory": "Dementia",
                        "epilepsy": "Epilepsy",
                        "seizure": "Epilepsy",
                        "vascular": "Vascular neurology stroke",
                        "stroke": "Vascular neurology stroke",
                        "headache": "Headache",
                        "migraine": "Headache",
                        "movement": "Movement Disorders",
                        "parkinson": "Movement Disorders",
                        "tremor": "Movement Disorders",
                        "dystonia": "Movement Disorders",
                        "muscle": "Neuromuscular",
                        "neuropathy": "Neuromuscular",
                        "myopathy": "Neuromuscular",
                        "infection": "Neuro infectious",
                        "meningitis": "Neuro infectious",
                        "encephalitis": "Neuro infectious",
                        "tumor": "Neuro oncology",
                        "cancer": "Neuro oncology",
                        "glioma": "Neuro oncology",
                        "mass": "Neuro oncology",
                        "multiple sclerosis": "Neuroimmunology",
                        "autoimmune": "Neuroimmunology",
                        "immune": "Neuroimmunology",
                        "sleep": "Sleep Neurology",
                        "narcolepsy": "Sleep Neurology",
                        "eye": "Neuroophthalmology",
                        "vision": "Neuroophthalmology",
                        "child": "Pediatric Neurology",
                        "pediatric": "Pediatric Neurology",
                        "toxin": "Neurotoxicology",
                        "toxic": "Neurotoxicology",
                        "psychiatric": "Neuropsychiatry",
                        "behavior": "Neuropsychiatry",
                        "anatomy": "Neuroanatomy",
                        "tract": "Neuroanatomy",
                        "critical": "Critical Care Neurology",
                        "emergency": "Critical Care Neurology",
                        "coma": "Critical Care Neurology",
                        "genetic": "Neurogenetics"
                    }
                    
                    # Check classification reason for keywords
                    if explanation:
                        # Get the lowercase version for case-insensitive matching
                        explanation_lower = explanation.lower()
                        
                        # Try to find matching keywords in the explanation
                        for keyword, related_subspecialty in subspecialty_keywords.items():
                            if keyword in explanation_lower:
                                subspecialty = related_subspecialty
                                break
                                
                        # Also try to extract from source file name if it has subspecialty indicators
                        if "Dementia" in source_file:
                            subspecialty = "Dementia"
                        elif "Stroke" in source_file or "Vascular" in source_file:
                            subspecialty = "Vascular neurology stroke"
                        # Add more source file pattern matching as needed
                    
                    # Check if the MCQ already exists
                    if MCQ.objects.filter(question_number=question_number, question_text=question_text).exists():
                        existing_count += 1
                        continue
                    
                    # Convert options to JSON
                    options_json = json.dumps(options)
                    
                    # Create the MCQ without the classification reason
                    # Don't store the Classification Reason as per user request
                    MCQ.objects.create(
                        question_number=question_number,
                        question_text=question_text,
                        options=options_json,
                        correct_answer=correct_answer or "A",
                        subspecialty=subspecialty,
                        source_file=source_file,
                        exam_type=exam_type,
                        exam_year=exam_year,
                        explanation="" # No classification reason saved
                    )
                    imported_count += 1
                
                messages.success(request, f'Successfully imported {imported_count} MCQs. {existing_count} were already in the database.')
                return redirect('dashboard')
                
            except Exception as e:
                messages.error(request, f'Error importing MCQs from text: {str(e)}')
        
        else:
            messages.error(request, 'Please provide either a JSON file or MCQ text to import.')
    
    return render(request, 'mcq/import_form.html', {'subspecialties': SUBSPECIALTIES})

# New ReasoningPal implementation - Cognitive Analysis Engine
@login_required
def reasoning_pal(request, mcq_id):
    """Dispatch reasoning analysis via the service layer."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    logger.info("New ReasoningPal request for MCQ #%s", mcq_id)

    selected_answer = request.POST.get('selected_answer')
    user_reasoning = (request.POST.get('user_reasoning', '') or '').strip()
    is_correct = request.POST.get('is_correct') == 'true'

    if not selected_answer:
        return JsonResponse({'error': 'Missing selected answer'}, status=400)

    if len(user_reasoning) < ReasoningService.MIN_REASON_LENGTH:
        return JsonResponse({
            'error': f'Please provide at least {ReasoningService.MIN_REASON_LENGTH} characters explaining your reasoning'
        }, status=400)

    mcq = get_object_or_404(MCQ, id=mcq_id)

    result = ReasoningService.start_analysis(
        request.user,
        mcq,
        selected_answer,
        user_reasoning,
        is_correct,
    )

    return JsonResponse(result.payload, status=result.status)

@login_required
def reasoning_next_step(request, session_id):
    """Advance to the next step in the reasoning guidance"""
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    from .models import CognitiveReasoningSession

    session = get_object_or_404(
        CognitiveReasoningSession,
        id=session_id,
        user=request.user,
    )

    result = ReasoningService.advance_step(session)
    return JsonResponse(result.payload, status=result.status)

@login_required
def reasoning_feedback(request, session_id):
    """Submit feedback for a reasoning session"""
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    from .models import CognitiveReasoningSession

    session = get_object_or_404(
        CognitiveReasoningSession,
        id=session_id,
        user=request.user,
    )

    feedback = request.POST.get('feedback')
    comments = request.POST.get('comments', '') or ''

    result = ReasoningService.submit_feedback(session, feedback, comments)
    return JsonResponse(result.payload, status=result.status)

@login_required
def check_reasoning(request, mcq_id):
    """Check if the reasoning analysis is complete"""
    placeholder_id = request.GET.get('id', '')
    
    # Get the latest reasoning session for this MCQ and user
    try:
        session = ReasoningSession.objects.filter(
            mcq_id=mcq_id, 
            user=request.user
        ).latest('created_at')
    except ReasoningSession.DoesNotExist:
        return JsonResponse({
            'ready': False,
            'error': 'Session not found'
        })
    
    # Check if the guidance is still a placeholder
    if session.guidance and f'id="reasoning-{placeholder_id}"' in session.guidance:
        # Debug log the check
        logger.debug(f"Checking reasoning status for MCQ #{mcq_id}, placeholder_id: {placeholder_id}")
        logger.debug(f"Still processing, found placeholder marker in guidance")
        return JsonResponse({
            'ready': False
        })
    
    # Guidance is ready
    logger.debug(f"Reasoning is ready for MCQ #{mcq_id}, returning guidance of length: {len(session.guidance)}")
    return JsonResponse({
        'ready': True,
        'guidance': session.guidance
    })

@login_required
def reasoning_feedback(request, session_id):
    """View to handle feedback on ReasoningPal sessions"""
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    
    # Get the session
    session = get_object_or_404(ReasoningSession, id=session_id, user=request.user)
    
    # Get feedback data
    satisfaction_level = request.POST.get('satisfaction_level')
    follow_up_question = request.POST.get('follow_up_question', '')
    
    # Update the session
    session.satisfaction_level = satisfaction_level
    session.save()
    
    # Check if OpenAI integration is available
    from .openai_integration import api_key, client
    follow_up_answer = None
    
    # Handle follow-up question if provided
    if follow_up_question:
        if not api_key or not client:
            follow_up_answer = """<div class='alert alert-warning'>
                <h4><i class='bi bi-exclamation-triangle'></i> AI Features Unavailable</h4>
                <p>This feature requires OpenAI API access, which is not currently configured.</p>
                <p>Please contact the administrator to set up the OpenAI API key.</p>
            </div>"""
        else:
            # Use the MCQ from the session to answer the follow-up
            mcq = session.mcq
            follow_up_answer = answer_question_about_mcq(mcq, follow_up_question)
    
    return JsonResponse({
        'status': 'feedback recorded',
        'follow_up_answer': follow_up_answer
    })

@login_required
def check_reasoning_task_status(request, session_id):
    """Check the status of a background reasoning analysis task"""
    from .models import CognitiveReasoningSession

    session = get_object_or_404(
        CognitiveReasoningSession,
        id=session_id,
        user=request.user,
    )

    result = ReasoningService.check_task_status(session)
    return JsonResponse(result.payload, status=result.status)

@login_required
def test_worker_connectivity(request):
    """Test endpoint to check if Celery workers are available"""
    if not request.user.is_superuser:
        return JsonResponse({'error': 'Admin access required'}, status=403)
    
    try:
        from celery import current_app
        from .tasks import process_clinical_reasoning_analysis
        
        # Check if we can inspect active workers
        inspect = current_app.control.inspect()
        active_workers = inspect.active()
        
        # Check if our task is registered
        registered_tasks = inspect.registered()
        
        diagnostics = {
            'celery_app_name': current_app.main,
            'broker_url': current_app.conf.broker_url,
            'result_backend': current_app.conf.result_backend,
            'active_workers': active_workers or {},
            'registered_tasks': registered_tasks or {},
            'task_available': any(
                'mcq.tasks.process_clinical_reasoning_analysis' in tasks 
                for tasks in (registered_tasks or {}).values()
            ) if registered_tasks else False,
            'worker_count': len(active_workers) if active_workers else 0
        }
        
        return JsonResponse({
            'success': True,
            'status': 'connected',
            'diagnostics': diagnostics
        })
        
    except Exception as e:
        logger.error(f"Worker connectivity test failed: {e}")
        return JsonResponse({
            'success': False,
            'error': str(e),
            'status': 'failed'
        })

@login_required
def check_failed_sessions(request):
    """Check for recent failed background task sessions"""
    if not request.user.is_superuser:
        return JsonResponse({'error': 'Admin access required'}, status=403)

    result = ReasoningService.failed_session_summary()
    return JsonResponse(result.payload, status=result.status)

@login_required
@csrf_exempt
def generate_test_question(request, mcq_id):
    """Generate a test question based on the ReasoningPal guidance"""
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    
    # Get the MCQ
    mcq = get_object_or_404(MCQ, id=mcq_id)
    
    # Get the guidance content from the request
    guidance_content = request.POST.get('guidance_content', '')
    
    # Import OpenAI utilities
    from .openai_integration import api_key, client, DEFAULT_MODEL, chat_completion, get_first_choice_text
    
    # Check if OpenAI API is available
    if not api_key or not client:
        # Return a simple mock question if API is not available
        return JsonResponse({
            'question': 'Based on the concepts discussed in the guidance, which of the following is most consistent with the described neurological condition?',
            'options': {
                'A': 'Option A - First potential diagnosis',
                'B': 'Option B - Second potential diagnosis',
                'C': 'Option C - Third potential diagnosis',
                'D': 'Option D - Fourth potential diagnosis'
            },
            'correct_answer': 'B',
            'correct_feedback': 'Correct! This option best matches the clinical description.',
            'incorrect_feedback': 'Let\'s analyze your reasoning to clarify this concept.',
            'detailed_explanation': 'This condition is characterized by specific features that distinguish it from other similar conditions.'
        })
    
    try:
        logger.info(f"Generating test question for MCQ #{mcq_id}")
        
        # Prepare the guidance content for the prompt
        guidance_summary = guidance_content[:1500]  # Limit to avoid token issues
        
        # Create a prompt for generating a related test question
        system_prompt = """You are a neurological education expert specializing in creating high-quality assessment questions. 
        Your task is to create a new question that tests understanding of the same concept covered in the guidance material.
        
        Create a question that:
        1. Tests the deeper understanding of the concept, not just recall
        2. Is related to the same topic but NOT identical to the original question
        3. Requires application of the principles explained in the guidance
        4. Has one clearly correct answer and plausible distractors
        
        Your response must be in valid JSON format with these exact keys:
        {
            "question": "The question text here",
            "options": {
                "A": "First option",
                "B": "Second option",
                "C": "Third option",
                "D": "Fourth option"
            },
            "correct_answer": "The letter (A-D) of the correct option",
            "correct_feedback": "Feedback to show when the user answers correctly",
            "incorrect_feedback": "Initial feedback to show when the user answers incorrectly",
            "detailed_explanation": "A detailed explanation of why the correct answer is right and why other options are wrong"
        }
        """
        
        user_prompt = f"""Based on the following neurological guidance content, create a related test question:

        GUIDANCE CONTENT:
        {guidance_summary}
        
        Create a new clinically relevant question that tests understanding of the same neurological concept.
        The question should:
        - Be focused on the same condition or diagnostic approach
        - Require application of the principles in the guidance
        - Test deeper understanding, not just recall
        - Include 4 options (A, B, C, D) with one clearly correct answer
        - Provide appropriate feedback for both correct and incorrect responses
        
        Return ONLY valid JSON following the format specified in the system instructions.
        """
        
        # Call OpenAI API (GPT-5 compatible token param via wrapper)
        response = chat_completion(
            client,
            DEFAULT_MODEL,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            max_tokens=1500,
            temperature=0.7,  # Higher temperature for more creative questions
            top_p=0.95,
            response_format={"type": "json_object"}
        )
        
        # Parse the API response
        import json
        response_text = get_first_choice_text(response)
        if not response_text:
            raise ValueError('OpenAI returned empty content while generating test question')
        response_json = json.loads(response_text)
        
        # Validate the response has all required fields
        required_fields = ["question", "options", "correct_answer", "correct_feedback", "incorrect_feedback", "detailed_explanation"]
        if not all(field in response_json for field in required_fields):
            logger.warning(f"Missing fields in OpenAI response: {response_json}")
            raise ValueError("The API response is missing required fields")
        
        # Return the generated test question
        return JsonResponse(response_json)
        
    except Exception as e:
        logger.error(f"Error generating test question: {str(e)}")
        
        # Return a fallback question if API fails
        return JsonResponse({
            'question': 'Based on the neurological concepts discussed, which of the following best explains the underlying pathophysiology?',
            'options': {
                'A': 'Demyelination of central nervous system axons',
                'B': 'Degeneration of motor neurons',
                'C': 'Antibody-mediated blockade of neuromuscular junction',
                'D': 'Inflammation of peripheral nerve myelin sheaths'
            },
            'correct_answer': 'B',
            'correct_feedback': 'Well done! You\'ve correctly identified the underlying mechanism.',
            'incorrect_feedback': 'Let\'s review the core concepts to clarify this topic.',
            'detailed_explanation': 'The correct answer relates to the progressive degeneration pattern described in the guidance.'
        })

@login_required
@csrf_exempt
def analyze_test_reasoning(request, mcq_id):
    """Analyze the user's reasoning for their test answer"""
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    
    # Get the MCQ
    mcq = get_object_or_404(MCQ, id=mcq_id)
    
    # Get the user's reasoning and answer details
    user_reasoning = request.POST.get('user_reasoning', '')
    selected_answer = request.POST.get('selected_answer', '')
    correct_answer = request.POST.get('correct_answer', '')
    
    # Import OpenAI utilities
    from .openai_integration import api_key, client, DEFAULT_MODEL, chat_completion
    
    # Check if OpenAI API is available
    if not api_key or not client:
        # Return a simple mock response if API is not available
        return JsonResponse({
            'detailed_feedback': """
                <p>Based on your explanation, here are the key points to understand:</p>
                <ul class="mb-0">
                    <li>The correct option is supported by specific clinical features</li>
                    <li>The pathophysiology involves characteristic mechanisms</li>
                    <li>This condition can be distinguished from others by its specific patterns</li>
                    <li>Understanding these core concepts will help with similar questions</li>
                </ul>
            """
        })

@login_required
@csrf_exempt
def generate_test_question_async(request, mcq_id):
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    guidance_content = request.POST.get('guidance_content', '')
    job_id = str(uuid.uuid4())
    cache.set(f"ai_job:{job_id}", {'status': 'pending'}, timeout=3600)
    try:
        from .tasks import run_ai_job
        run_ai_job.delay(job_id, 'generate_test_question', {
            'mcq_id': mcq_id,
            'guidance_content': guidance_content,
            'user_id': request.user.id,
        })
        return JsonResponse({'job_id': job_id, 'status': 'queued'})
    except Exception as e:
        return JsonResponse({'error': f'Failed to enqueue: {str(e)}'}, status=500)

@login_required
@csrf_exempt
def analyze_test_reasoning_async(request, mcq_id):
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    user_reasoning = request.POST.get('user_reasoning', '')
    selected_answer = request.POST.get('selected_answer', '')
    correct_answer = request.POST.get('correct_answer', '')

    # Build question/options text from DB for context
    mcq = get_object_or_404(MCQ, id=mcq_id)
    options_dict = mcq.get_options_dict()
    options_text = "\n".join(f"{k}. {v}" for k, v in options_dict.items())

    job_id = str(uuid.uuid4())
    cache.set(f"ai_job:{job_id}", {'status': 'pending'}, timeout=3600)
    try:
        from .tasks import run_ai_job
        run_ai_job.delay(job_id, 'analyze_test_reasoning', {
            'mcq_id': mcq_id,
            'question_text': mcq.question_text,
            'options_text': options_text,
            'correct_answer': correct_answer or mcq.correct_answer,
            'user_reasoning': user_reasoning,
            'selected_answer': selected_answer,
            'user_id': request.user.id,
        })
        return JsonResponse({'job_id': job_id, 'status': 'queued'})
    except Exception as e:
        return JsonResponse({'error': f'Failed to enqueue: {str(e)}'}, status=500)
    
    try:
        logger.info(f"Analyzing test reasoning for MCQ #{mcq_id}")
        
        # Create a prompt for analyzing the user's reasoning
        system_prompt = """You are a neurological education expert who provides personalized feedback on learners' clinical reasoning.
        Analyze the learner's reasoning for their answer choice and provide detailed, educational feedback.
        
        Your response should:
        1. Identify both strengths and gaps in their understanding
        2. Correct specific misconceptions in their reasoning
        3. Explain why the correct answer is right using evidence-based explanations
        4. Provide a framework for thinking about similar questions
        
        Format your response with HTML for direct display in a web interface:
        - Use <p> tags for paragraphs
        - Use <ul> and <li> tags for lists
        - Use <strong> tags for emphasis
        - Keep formatting clean and web-friendly
        """
        
        user_prompt = f"""I need to provide feedback on a learner's reasoning about a test question.

        BACKGROUND:
        - The correct answer is option {correct_answer}
        - The learner selected option {selected_answer} 
        - The question tests understanding of neurological concepts
        
        LEARNER'S REASONING:
        {user_reasoning}
        
        Please analyze their reasoning and provide detailed feedback. Include:
        1. What parts of their reasoning show good understanding
        2. What specific misconceptions or knowledge gaps they have
        3. Why option {correct_answer} is correct (with clinical reasoning)
        4. How to distinguish between similar neurological concepts
        
        Format your response with HTML for direct display on a website, including appropriate paragraph tags, bullet points, and emphasis.
        """
        
        # Call OpenAI API (GPT-5 compatible token param via wrapper)
        response = chat_completion(
            client,
            DEFAULT_MODEL,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            max_tokens=1000,
            temperature=0.4,
            top_p=0.95
        )
        
        # Return the detailed feedback
        feedback_html = get_first_choice_text(response)
        if not feedback_html:
            raise ValueError('OpenAI returned empty feedback content')
        return JsonResponse({
            'detailed_feedback': feedback_html
        })
        
    except Exception as e:
        logger.error(f"Error analyzing test reasoning: {str(e)}")
        
        # Return a fallback response if API fails
        return JsonResponse({
            'detailed_feedback': f"""
                <p>Thank you for sharing your reasoning. Here's some feedback:</p>
                <ul>
                    <li>Option {correct_answer} is correct because it best aligns with the neurological principles discussed</li>
                    <li>The key is to focus on the specific pattern of symptoms and their progression</li>
                    <li>Understanding the underlying pathophysiology helps distinguish between similar conditions</li>
                    <li>Continue applying these concepts to build your diagnostic reasoning skills</li>
                </ul>
            """
        })
    
@login_required
@require_POST
def hide_mcq(request, mcq_id):
    """Hide an MCQ for the current user"""
    mcq = get_object_or_404(MCQ, id=mcq_id)
    
    # Get the subspecialty for redirecting back after hiding
    subspecialty = mcq.subspecialty
    
    # Check if HiddenMCQ model is available
    if HiddenMCQ is None:
        messages.warning(request, "The hidden MCQ feature is not available in this version.")
        return redirect('view_mcq', mcq_id=mcq_id)
    
    # Create HiddenMCQ entry or get existing one
    hidden_mcq, created = HiddenMCQ.objects.get_or_create(
        user=request.user,
        mcq=mcq
    )
    
    # Show appropriate message
    if created:
        messages.success(request, f"MCQ #{mcq_id} has been hidden from your view.")
    else:
        messages.info(request, f"MCQ #{mcq_id} was already hidden.")
    
    # Redirect to the subspecialty page
    return redirect('subspecialty', subspecialty=subspecialty)
    
@login_required
@require_POST
def unhide_mcq(request, mcq_id):
    """Unhide a previously hidden MCQ"""
    mcq = get_object_or_404(MCQ, id=mcq_id)
    
    # Check if HiddenMCQ model is available
    if HiddenMCQ is None:
        messages.warning(request, "The hidden MCQ feature is not available in this version.")
        return redirect('view_mcq', mcq_id=mcq_id)
    
    # Try to find and delete the HiddenMCQ entry
    try:
        hidden_mcq = HiddenMCQ.objects.get(user=request.user, mcq=mcq)
        hidden_mcq.delete()
        messages.success(request, f"MCQ #{mcq_id} is now visible again.")
    except HiddenMCQ.DoesNotExist:
        messages.info(request, f"MCQ #{mcq_id} was not hidden.")
    
    # Check if we should redirect to hidden MCQs list or detail view
    if request.GET.get('from') == 'hidden_list':
        return redirect('view_hidden_mcqs')
    
    # Redirect back to the MCQ detail view
    return redirect('view_mcq', mcq_id=mcq_id)

@login_required
def view_hidden_mcqs(request):
    """View all hidden MCQs for the current user"""
    # Safety check if HiddenMCQ model is available
    if HiddenMCQ is None:
        messages.warning(request, "Hidden MCQ feature is not available in this version.")
        return redirect('dashboard')
    
    # Get all hidden MCQs for this user with related MCQ objects
    hidden_mcqs = HiddenMCQ.objects.filter(
        user=request.user
    ).select_related('mcq').order_by('mcq__subspecialty', 'mcq__id')
    
    if not hidden_mcqs:
        messages.info(request, "You don't have any hidden MCQs.")
        return redirect('dashboard')
    
    # Group by subspecialty for better organization
    subspecialties = {}
    for hidden_mcq in hidden_mcqs:
        mcq = hidden_mcq.mcq
        if mcq.subspecialty not in subspecialties:
            subspecialties[mcq.subspecialty] = []
        subspecialties[mcq.subspecialty].append(mcq)
    
    context = {
        'hidden_mcqs': hidden_mcqs,
        'subspecialties': subspecialties,
        'total_count': hidden_mcqs.count()
    }
    
    return render(request, 'mcq/hidden_mcqs.html', context)


@login_required
@require_POST
def report_question(request, mcq_id):
    """Allow users to report issues with MCQ questions."""
    mcq = get_object_or_404(MCQ, id=mcq_id)
    
    # Check if user has already reported this question
    existing_report = QuestionReport.objects.filter(
        question=mcq,
        user=request.user,
        status='pending'
    ).first()
    
    if existing_report:
        messages.warning(request, "You have already reported this question and it's pending review.")
        return redirect('view_mcq', mcq_id=mcq_id)
    
    form = QuestionReportForm(request.POST)
    
    if form.is_valid():
        report = form.save(commit=False)
        report.question = mcq
        report.user = request.user
        report.save()
        
        messages.success(request, "Thank you for reporting this question. Our team will review it.")
        return redirect('view_mcq', mcq_id=mcq_id)
    else:
        messages.error(request, "There was an error with your report. Please try again.")
        return redirect('view_mcq', mcq_id=mcq_id)


@login_required
def show_report_form(request, mcq_id):
    """Show the report form for an MCQ."""
    mcq = get_object_or_404(MCQ, id=mcq_id)
    
    # Check if user has already reported this question
    existing_report = QuestionReport.objects.filter(
        question=mcq,
        user=request.user,
        status='pending'
    ).first()
    
    if existing_report:
        messages.warning(request, "You have already reported this question and it's pending review.")
        return redirect('view_mcq', mcq_id=mcq_id)
    
    form = QuestionReportForm()
    
    context = {
        'mcq': mcq,
        'form': form,
        'options': mcq.get_options_dict()
    }
    
    return render(request, 'mcq/report_question.html', context)


@staff_member_required
def admin_export_page(request):
    """Admin page for exporting MCQ data"""
    # Get counts for all subspecialties
    subspecialty_counts = {}
    
    for display_name, db_name in SUBSPECIALTY_MAPPING.items():
        count = MCQ.objects.filter(subspecialty=db_name).count()
        subspecialty_counts[display_name] = count
    
    context = {
        'subspecialty_counts': subspecialty_counts,
        'total_count': MCQ.objects.count(),
        'subspecialties': SUBSPECIALTIES,
    }
    
    return render(request, 'mcq/admin_export.html', context)


@staff_member_required
def export_vascular_mcqs(request):
    """Export vascular neurology MCQs as CSV for download"""
    # Query for vascular neurology MCQs
    vascular_mcqs = MCQ.objects.filter(
        subspecialty__in=['Vascular Neurology/Stroke', 'Vascular Neurology', 'Stroke']
    ).order_by('exam_type', 'exam_year', 'question_number')
    
    # Create the HttpResponse object with CSV headers
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="vascular_mcqs_{timezone.now().strftime("%Y%m%d_%H%M%S")}.csv"'
    
    writer = csv.writer(response)
    
    # Write headers
    headers = [
        'ID', 'Question Number', 'Exam Type', 'Exam Year', 'Question Text',
        'Option A', 'Option B', 'Option C', 'Option D', 'Option E', 'Option F',
        'Correct Answer', 'Subspecialty', 'Conceptual Foundation',
        'Pathophysiology', 'Clinical Correlation', 'Diagnostic Approach',
        'Classification and Neurology', 'Management Principles',
        'Option Analysis', 'Clinical Pearls', 'Current Evidence',
        'Has Image', 'Image URL', 'Source File'
    ]
    writer.writerow(headers)
    
    # Write MCQ data
    for mcq in vascular_mcqs:
        options = mcq.get_options_dict()
        explanation_sections = mcq.explanation_sections or {}
        
        def get_section(keys):
            for key in keys:
                if key in explanation_sections and explanation_sections[key]:
                    value = explanation_sections[key]
                    # Check if value is a string before using string methods
                    if isinstance(value, str):
                        return value.replace('\n', ' ').strip()
                    elif isinstance(value, dict):
                        # If it's a dictionary, convert to JSON string
                        return json.dumps(value).replace('\n', ' ')
                    else:
                        # Convert any other type to string
                        return str(value)
            return ''
        
        row = [
            mcq.id,
            mcq.question_number or '',
            mcq.exam_type or '',
            mcq.exam_year or '',
            mcq.question_text,
            options.get('A', ''),
            options.get('B', ''),
            options.get('C', ''),
            options.get('D', ''),
            options.get('E', ''),
            options.get('F', ''),
            mcq.correct_answer,
            mcq.subspecialty,
            get_section(['conceptual foundation', 'conceptual_foundation']),
            get_section(['pathophysiology', 'pathophysiological_mechanisms']),
            get_section(['clinical correlation', 'clinical_correlation', 'clinical context']),
            get_section(['diagnostic approach', 'diagnostic_approach']),
            get_section(['classification and neurology', 'classification_neurology', 'classification_and_nosology']),
            get_section(['management principles', 'management_principles']),
            get_section(['option analysis', 'option_analysis']),
            get_section(['clinical pearls', 'clinical_pearls', 'key insight']),
            get_section(['current evidence', 'current_evidence', 'quick reference']),
            'Yes' if mcq.image_url else 'No',
            mcq.image_url or '',
            mcq.source_file or ''
        ]
        writer.writerow(row)
    
    return response


@staff_member_required
def export_subspecialty_mcqs(request, subspecialty):
    """Export MCQs for a specific subspecialty as CSV for download"""
    # Map display name to database name
    db_subspecialty = SUBSPECIALTY_MAPPING.get(subspecialty, subspecialty)
    
    # Query for MCQs in this subspecialty
    mcqs = MCQ.objects.filter(
        subspecialty=db_subspecialty
    ).order_by('exam_type', 'exam_year', 'question_number')
    
    # Create the HttpResponse object with CSV headers
    response = HttpResponse(content_type='text/csv')
    
    # Safely convert subspecialty to string and create filename
    safe_name = str(subspecialty).lower()
    safe_name = safe_name.replace('/', '_').replace(' ', '_').replace('-', '_')
    safe_name = re.sub(r'[^\w_]', '', safe_name)  # Remove any remaining non-alphanumeric chars
    
    filename = f"{safe_name}_mcqs_{timezone.now().strftime('%Y%m%d_%H%M%S')}.csv"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    
    writer = csv.writer(response)
    
    # Write headers
    headers = [
        'ID', 'Question Number', 'Exam Type', 'Exam Year', 'Question Text',
        'Option A', 'Option B', 'Option C', 'Option D', 'Option E', 'Option F',
        'Correct Answer', 'Subspecialty', 'Conceptual Foundation',
        'Pathophysiology', 'Clinical Correlation', 'Diagnostic Approach',
        'Classification and Neurology', 'Management Principles',
        'Option Analysis', 'Clinical Pearls', 'Current Evidence',
        'Has Image', 'Image URL', 'Source File'
    ]
    writer.writerow(headers)
    
    # Write MCQ data
    for mcq in mcqs:
        options = mcq.get_options_dict()
        explanation_sections = mcq.explanation_sections or {}
        
        def get_section(keys):
            for key in keys:
                if key in explanation_sections and explanation_sections[key]:
                    value = explanation_sections[key]
                    # Check if value is a string before using string methods
                    if isinstance(value, str):
                        return value.replace('\n', ' ').strip()
                    elif isinstance(value, dict):
                        # If it's a dictionary, convert to JSON string
                        return json.dumps(value).replace('\n', ' ')
                    else:
                        # Convert any other type to string
                        return str(value)
            return ''
        
        row = [
            mcq.id,
            mcq.question_number or '',
            mcq.exam_type or '',
            mcq.exam_year or '',
            mcq.question_text,
            options.get('A', ''),
            options.get('B', ''),
            options.get('C', ''),
            options.get('D', ''),
            options.get('E', ''),
            options.get('F', ''),
            mcq.correct_answer,
            mcq.subspecialty,
            get_section(['conceptual foundation', 'conceptual_foundation']),
            get_section(['pathophysiology', 'pathophysiological_mechanisms']),
            get_section(['clinical correlation', 'clinical_correlation', 'clinical context']),
            get_section(['diagnostic approach', 'diagnostic_approach']),
            get_section(['classification and neurology', 'classification_neurology', 'classification_and_nosology']),
            get_section(['management principles', 'management_principles']),
            get_section(['option analysis', 'option_analysis']),
            get_section(['clinical pearls', 'clinical_pearls', 'key insight']),
            get_section(['current evidence', 'current_evidence', 'quick reference']),
            'Yes' if mcq.image_url else 'No',
            mcq.image_url or '',
            mcq.source_file or ''
        ]
        writer.writerow(row)
    
    return response

@staff_member_required
@csrf_exempt
def clear_mcqs(request):
    """Clear all MCQs from the database"""
    if request.method == 'POST':
        try:
            deleted_count = MCQ.objects.all().delete()[0]
            return JsonResponse({
                'status': 'success',
                'deleted': deleted_count,
                'message': f'Cleared {deleted_count} MCQs'
            })
        except Exception as e:
            return JsonResponse({
                'status': 'error',
                'message': str(e)
            }, status=500)
    
    return JsonResponse({'status': 'error', 'message': 'Method not allowed'}, status=405)


@staff_member_required  
@csrf_exempt
def import_mcqs_batch(request):
    """Import MCQs in batch via JSON POST"""
    if request.method == 'POST':
        try:
            import json
            data = json.loads(request.body.decode('utf-8'))
            mcqs = data.get('mcqs', [])
            batch_info = data.get('batch_info', {})
            
            imported_count = 0
            errors = []
            
            for mcq_data in mcqs:
                try:
                    # Process explanation sections
                    explanation_sections = {}
                    if 'explanation' in mcq_data and isinstance(mcq_data['explanation'], dict):
                        explanation_sections = mcq_data['explanation']
                    
                    # Check if MCQ already exists
                    exists = MCQ.objects.filter(
                        question_number=mcq_data.get('question_number'),
                        exam_type=mcq_data.get('exam_type'),
                        year=mcq_data.get('year'),
                        specialty=mcq_data.get('specialty'),
                        subspecialty=mcq_data.get('subspecialty')
                    ).exists()
                    
                    if not exists:
                        MCQ.objects.create(
                            question_number=mcq_data.get('question_number'),
                            exam_type=mcq_data.get('exam_type'),
                            year=mcq_data.get('year'),
                            question_text=mcq_data.get('question_text', ''),
                            options=mcq_data.get('options', []),
                            correct_answer=mcq_data.get('correct_answer', ''),
                            explanation_sections=explanation_sections,
                            specialty=mcq_data.get('specialty', 'Neurology'),
                            subspecialty=mcq_data.get('subspecialty', 'General Neurology'),
                            topic=mcq_data.get('topic', '')
                        )
                        imported_count += 1
                        
                except Exception as e:
                    errors.append(f"Error importing MCQ {mcq_data.get('question_number', 'unknown')}: {str(e)}")
            
            return JsonResponse({
                'status': 'success',
                'imported': imported_count,
                'errors': errors,
                'batch_info': batch_info
            })
            
        except Exception as e:
            return JsonResponse({
                'status': 'error',
                'message': str(e)
            }, status=500)
    
    return JsonResponse({'status': 'error', 'message': 'Method not allowed'}, status=405)


# Case Learning Session Management Views

@login_required
def list_case_sessions(request):
    """List all active AI-driven case sessions for the current user."""

    sessions = (
        PersistentCaseLearningSession.objects.filter(
            user=request.user, archived=False, completed=False
        )
        .order_by("-last_activity")
    )

    def _serialize(session: PersistentCaseLearningSession) -> Dict[str, Any]:
        meta = session.case_data or {}
        return {
            "session_id": session.session_id,
            "specialty": session.specialty,
            "difficulty": session.difficulty,
            "state_label": meta.get("phase", "CONVERSATION"),
            "last_activity": session.last_activity.isoformat(),
            "created_at": session.created_at.isoformat(),
        }

    payload = [_serialize(session) for session in sessions]
    return JsonResponse({"sessions": payload, "count": len(payload)})

@login_required
@csrf_exempt
def resume_case_session(request):
    """Resume a specific case learning session."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    
    try:
        data = json.loads(request.body)
        session_id = data.get('session_id')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    if not session_id:
        return JsonResponse({'error': 'Session ID required'}, status=400)

    try:
        result = case_conversation_service.resume_session(
            user=request.user, session_id=session_id
        )
    except PermissionDenied:
        return JsonResponse({'error': 'Session not found'}, status=404)
    except ValueError as exc:
        return JsonResponse({'error': str(exc)}, status=400)
    except RuntimeError as exc:
        logger.exception("Case resume runtime error")
        return JsonResponse({'error': str(exc)}, status=500)
    except Exception as exc:  # pragma: no cover
        logger.exception("Unexpected resume failure")
        return JsonResponse({'error': 'Unexpected error'}, status=500)

    return JsonResponse(result)

@login_required
@csrf_exempt  
def delete_case_session(request):
    """Delete a specific case learning session"""
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    
    try:
        data = json.loads(request.body)
        session_id = data.get('session_id')
        
        if not session_id:
            return JsonResponse({'error': 'Session ID required'}, status=400)
        
        # Delete the session
        deleted_count, _ = PersistentCaseLearningSession.objects.filter(
            session_id=session_id,
            user=request.user
        ).delete()
        
        if deleted_count == 0:
            return JsonResponse({'error': 'Session not found'}, status=404)
        
        return JsonResponse({
            'status': 'success',
            'message': 'Session deleted successfully'
        })
        
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@login_required
def debug_mcq_ids(request):
    """Debug view to check MCQ IDs - TEMPORARY"""
    from django.http import JsonResponse
    from .models import MCQ
    from django.db.models import Min, Max
    
    # Get basic stats
    total = MCQ.objects.count()
    if total == 0:
        return JsonResponse({'error': 'No MCQs in database'})
    
    # Get ID range
    id_stats = MCQ.objects.aggregate(min_id=Min('id'), max_id=Max('id'))
    
    # Check for the specific IDs from 404 errors
    test_ids = [100090767, 100090772]
    id_checks = {}
    for test_id in test_ids:
        id_checks[str(test_id)] = MCQ.objects.filter(id=test_id).exists()
    
    # Get sample MCQs from Neuro-infectious
    neuro_infectious_samples = list(
        MCQ.objects.filter(subspecialty='Neuro-infectious')
        .order_by('-id')
        .values('id', 'question_text')[:5]
    )
    
    # Check for large IDs
    large_id_count = MCQ.objects.filter(id__gte=100000000).count()
    
    return JsonResponse({
        'total_mcqs': total,
        'id_range': id_stats,
        'specific_id_checks': id_checks,
        'large_id_count': large_id_count,
        'neuro_infectious_samples': neuro_infectious_samples,
        'subspecialty_variations': list(
            MCQ.objects.filter(subspecialty__icontains='neuro-infect')
            .values_list('subspecialty', flat=True)
            .distinct()
        )
    })


# Inline MCQ editing AJAX views

@login_required
@staff_member_required
@require_POST
@csrf_exempt
def update_mcq_question(request, mcq_id):
    """Update MCQ question text via AJAX"""
    mcq = get_object_or_404(MCQ, id=mcq_id)
    
    try:
        data = json.loads(request.body)
        question_text = data.get('question_text', '').strip()
        
        if not question_text:
            return JsonResponse({'error': 'Question text cannot be empty'}, status=400)
        
        # Update the question
        mcq.question_text = question_text
        mcq.save()
        
        return JsonResponse({
            'success': True,
            'message': 'Question updated successfully',
            'question_text': mcq.question_text
        })
        
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON data'}, status=400)
    except ValueError as e:
        return JsonResponse({'success': False, 'error': str(e)})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@login_required
@staff_member_required
@require_POST
@csrf_exempt
def update_mcq_options(request, mcq_id):
    """Update MCQ options and correct answer via AJAX"""
    logger.info(f"Updating options for MCQ #{mcq_id}")
    
    try:
        mcq = get_object_or_404(MCQ, id=mcq_id)
        logger.info(f"Found MCQ #{mcq_id}: {mcq.question_text[:50]}...")
    except Exception as e:
        logger.error(f"MCQ #{mcq_id} not found: {str(e)}")
        return JsonResponse({'error': f'MCQ not found: {str(e)}'}, status=404)
    
    try:
        data = json.loads(request.body)
        logger.info(f"Parsed request data for MCQ #{mcq_id}")
        
        # Get options
        option_a = data.get('option_a', '').strip()
        option_b = data.get('option_b', '').strip()
        option_c = data.get('option_c', '').strip()
        option_d = data.get('option_d', '').strip()
        correct_answer = data.get('correct_answer', '').strip()
        
        # Validate required fields
        if not all([option_a, option_b, option_c, option_d, correct_answer]):
            return JsonResponse({'error': 'All options and correct answer are required'}, status=400)
        
        # Create options dictionary  
        options_dict = {'A': option_a, 'B': option_b, 'C': option_c, 'D': option_d}
        
        # Update options as dictionary (better format for get_options_dict)
        mcq.options = options_dict
        mcq.correct_answer = correct_answer
        
        # Update correct answer text
        correct_answer_letters = [letter.strip() for letter in correct_answer.split(',')]
        correct_texts = []
        
        for letter in correct_answer_letters:
            if letter in options_dict:
                correct_texts.append(options_dict[letter])
        
        mcq.correct_answer_text = ', '.join(correct_texts) if correct_texts else ''
        
        # Check if auto-regenerate explanations is requested (default: False for safety)  
        auto_regenerate = data.get('auto_regenerate_explanations', False)
        
        # Save the MCQ first
        mcq.save()
        logger.info(f"Successfully saved MCQ #{mcq_id} with new options")
        
        # Refresh from database to ensure we have the latest data
        mcq.refresh_from_db()
        
        # Auto-regenerate explanations if requested
        regenerated_explanations = ""
        regeneration_error = None
        if auto_regenerate:
            try:
                logger.info(f"Starting explanation regeneration for MCQ #{mcq_id}")
                from .openai_integration import regenerate_unified_explanation
                from .explanation_utils import render_explanation_as_html

                # Double-check that MCQ has the updated data
                logger.info(f"MCQ #{mcq_id} options before regeneration: {mcq.get_options_dict()}")
                logger.info(f"MCQ #{mcq_id} correct answer: {mcq.correct_answer}")

                regenerated_text = regenerate_unified_explanation(mcq)

                if regenerated_text and isinstance(regenerated_text, str):
                    regenerated_explanations = regenerated_text.strip()
                    mcq.unified_explanation = regenerated_explanations
                    mcq.explanation = regenerated_explanations
                    mcq.explanation_sections = None
                    mcq.save(update_fields=["unified_explanation", "explanation", "explanation_sections"])
                    logger.info(f"Auto-regenerated explanation for MCQ #{mcq_id} after options update")
                else:
                    logger.warning(f"Failed to regenerate explanations for MCQ #{mcq_id} - invalid response: {type(regenerated_explanations)}")
                    regeneration_error = "AI did not return a valid explanation"
            except ImportError as e:
                logger.error(f"Import error for regeneration function: {str(e)}")
                regeneration_error = "Failed to import regeneration function"
            except Exception as e:
                error_msg = str(e)
                logger.error(f"Error auto-regenerating explanations: {error_msg}")
                # Log the full traceback for debugging
                import traceback
                logger.error(f"Full traceback: {traceback.format_exc()}")
                regeneration_error = f"Regeneration error: {error_msg}"
        
        response_data = {
            'success': True,
            'message': 'Options and correct answer updated successfully',
            'options': mcq.get_options_dict(),  # Use the method to ensure consistent format
            'correct_answer': mcq.correct_answer,
            'correct_answer_text': mcq.correct_answer_text
        }
        
        if regenerated_explanations:
            from .explanation_utils import render_explanation_as_html
            response_data['unified_explanation'] = regenerated_explanations
            response_data['html_preview'] = render_explanation_as_html(regenerated_explanations)
            response_data['explanations_regenerated'] = True
            response_data['message'] += ' and explanation regenerated'
        elif auto_regenerate and regeneration_error:
            response_data['regeneration_error'] = regeneration_error
            response_data['explanations_regenerated'] = False
            response_data['message'] += ' (explanation regeneration failed)'
        
        return JsonResponse(response_data)
        
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error for MCQ #{mcq_id}: {str(e)}")
        return JsonResponse({'error': 'Invalid JSON data'}, status=400)
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Unexpected error updating MCQ #{mcq_id}: {error_msg}")
        # Log the full traceback for debugging
        import traceback
        logger.error(f"Full traceback: {traceback.format_exc()}")
        return JsonResponse({
            'error': f'Unexpected error: {error_msg}',
            'mcq_id': mcq_id
        }, status=500)


@login_required
@staff_member_required
@require_POST
@csrf_exempt
def update_mcq_explanation(request, mcq_id):
    """Update MCQ explanation via AJAX (unified text field)."""
    mcq = get_object_or_404(MCQ, id=mcq_id)

    try:
        data = json.loads(request.body)
        explanation_text = (data.get('explanation') or "").strip()
        sections_payload = data.get('sections') or {}
        try:
            from .explanation_sections import SECTION_LOOKUP
        except ImportError:
            SECTION_LOOKUP = {}

        # Backwards compatibility: older clients send structured sections.
        if not explanation_text and sections_payload:
            from .explanation_utils import merge_sections_to_text

            explanation_text = merge_sections_to_text(sections_payload)

        # Handle legacy individual section keys in payload.
        if not explanation_text and not sections_payload:
            for raw_key, raw_value in data.items():
                if raw_key in SECTION_LOOKUP and raw_value:
                    sections_payload[raw_key] = raw_value
            if sections_payload:
                from .explanation_utils import merge_sections_to_text

                explanation_text = merge_sections_to_text(sections_payload)

        explanation_text = explanation_text.strip()

        mcq.unified_explanation = explanation_text
        mcq.explanation = explanation_text
        mcq.explanation_sections = None
        mcq.save(update_fields=["unified_explanation", "explanation", "explanation_sections"])

        from .explanation_utils import render_explanation_as_html

        html_preview = render_explanation_as_html(explanation_text)

        return JsonResponse({
            'success': True,
            'message': 'Explanation updated successfully',
            'unified_explanation': explanation_text,
            'html_preview': html_preview,
        })

    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON data'}, status=400)
    except ValueError as e:
        return JsonResponse({'success': False, 'error': str(e)})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@login_required
@staff_member_required
@require_POST
@csrf_exempt
def update_mcq_image(request, mcq_id):
    """Update MCQ image URL via AJAX"""
    mcq = get_object_or_404(MCQ, id=mcq_id)
    
    try:
        data = json.loads(request.body)
        image_url = data.get('image_url', '').strip()
        
        # Validate URL if provided
        if image_url:
            if not (image_url.startswith('http://') or image_url.startswith('https://')):
                return JsonResponse({'error': 'Please enter a valid URL starting with http:// or https://'}, status=400)
        
        # Update image URL (model will handle Google Drive conversion)
        mcq.image_url = image_url
        mcq.save()
        
        return JsonResponse({
            'success': True,
            'message': 'Image URL updated successfully',
            'image_url': mcq.image_url
        })
        
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON data'}, status=400)
    except ValueError as e:
        return JsonResponse({'success': False, 'error': str(e)})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@login_required
@staff_member_required
@require_POST
@csrf_exempt
def update_mcq_metadata(request, mcq_id):
    """Update MCQ metadata (subspecialty, exam type, year) via AJAX"""
    mcq = get_object_or_404(MCQ, id=mcq_id)
    
    try:
        data = json.loads(request.body)
        
        # Get metadata fields
        subspecialty = data.get('subspecialty', '').strip()
        exam_type = data.get('exam_type', '').strip()
        exam_year = data.get('exam_year', '').strip()
        
        # Update fields if provided
        if subspecialty:
            mcq.subspecialty = subspecialty
        if exam_type:
            mcq.exam_type = exam_type
        if exam_year:
            try:
                mcq.exam_year = int(exam_year) if exam_year else None
            except ValueError:
                return JsonResponse({'error': 'Exam year must be a valid number'}, status=400)
        
        mcq.save()
        
        return JsonResponse({
            'success': True,
            'message': 'Metadata updated successfully',
            'subspecialty': mcq.subspecialty,
            'exam_type': mcq.exam_type,
            'exam_year': mcq.exam_year
        })
        
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON data'}, status=400)
    except ValueError as e:
        return JsonResponse({'success': False, 'error': str(e)})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# AI-powered editing views

@login_required
@staff_member_required
@require_POST
@csrf_exempt
def ai_edit_mcq_question(request, mcq_id):
    """Use AI to improve MCQ question clarity"""
    import logging
    logger = logging.getLogger(__name__)
    
    mcq = get_object_or_404(MCQ, id=mcq_id)
    
    try:
        data = json.loads(request.body)
        custom_instructions = data.get('custom_instructions', '').strip()
        
        logger.info(f"AI edit question request for MCQ #{mcq_id}, custom_instructions length: {len(custom_instructions)}")
        
        # Import AI function
        from .openai_integration import ai_edit_question, api_key, client
        
        # Check if OpenAI is available
        if not api_key or not client:
            return JsonResponse({
                'success': False,
                'error': 'OpenAI API is not configured. Please set the OPENAI_API_KEY environment variable.'
            }, status=503)
        
        # Get AI-improved question
        improved_question = ai_edit_question(mcq, custom_instructions)
        
        logger.info(f"AI edit question successful for MCQ #{mcq_id}")
        
        return JsonResponse({
            'success': True,
            'improved_text': improved_question,
            'message': 'AI has generated an improved question'
        })
        
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error in ai_edit_mcq_question: {e}")
        return JsonResponse({'error': 'Invalid JSON data'}, status=400)
    except ValueError as e:
        logger.error(f"Value error in ai_edit_mcq_question for MCQ #{mcq_id}: {e}")
        return JsonResponse({'success': False, 'error': str(e)})
    except Exception as e:
        logger.error(f"Unexpected error in ai_edit_mcq_question for MCQ #{mcq_id}: {e}", exc_info=True)
        return JsonResponse({'error': f'Internal server error: {str(e)}'}, status=500)


@login_required
@staff_member_required
@require_POST
@csrf_exempt
def ai_edit_mcq_options(request, mcq_id):
    """Use AI to improve MCQ options based on selected mode"""
    import logging
    logger = logging.getLogger(__name__)
    
    mcq = get_object_or_404(MCQ, id=mcq_id)
    
    try:
        data = json.loads(request.body)
        mode = data.get('mode', 'fill_missing')  # Default to fill_missing
        custom_instructions = data.get('custom_instructions', '').strip()
        draft_question_text = data.get('question_text', '')
        current_options_override = data.get('current_options')
        requested_correct_answer = data.get('correct_answer')
        
        logger.info(f"AI edit options request for MCQ #{mcq_id}, mode: {mode}, custom_instructions length: {len(custom_instructions)}")
        
        # Import AI functions
        from .openai_integration import ai_edit_options, ai_improve_all_options, regenerate_unified_explanation, api_key, client
        
        # Check if OpenAI is available
        if not api_key or not client:
            return JsonResponse({
                'success': False,
                'error': 'OpenAI API is not configured. Please set the OPENAI_API_KEY environment variable.'
            }, status=503)
        
        # Check if auto-regenerate explanations is requested (default: True)
        auto_regenerate = data.get('auto_regenerate_explanations', True)
        auto_apply = data.get('auto_apply', False)

        if isinstance(draft_question_text, str) and draft_question_text.strip():
            mcq.question_text = draft_question_text
        
        if isinstance(current_options_override, dict) and current_options_override:
            mcq.options = current_options_override

        if isinstance(requested_correct_answer, str) and requested_correct_answer.strip():
            mcq.correct_answer = requested_correct_answer.strip()
        
        # Get AI-improved options based on mode
        if mode == 'fill_missing':
            improved_options = ai_edit_options(mcq, custom_instructions)
        else:  # improve_all
            improved_options = ai_improve_all_options(mcq, custom_instructions)
        
        response_data = {
            'success': True,
            'options': improved_options,
            'message': f'AI has {"filled missing" if mode == "fill_missing" else "improved all"} options'
        }
        
        # If auto_apply is true, save the options and regenerate explanations
        if auto_apply and improved_options:
            try:
                # Update MCQ options
                mcq.options = improved_options
                mcq.save()
                
                # Refresh from database to ensure we have the latest data
                mcq.refresh_from_db()
                
                logger.info(f"Auto-applied AI-improved options to MCQ #{mcq_id}")
                
                # Auto-regenerate explanations if requested
                regeneration_error = None
                regenerated_explanations = ""
                if auto_regenerate:
                    try:
                        regenerated_text = regenerate_unified_explanation(mcq)

                        if regenerated_text and isinstance(regenerated_text, str):
                            regenerated_explanations = regenerated_text.strip()
                            mcq.unified_explanation = regenerated_explanations
                            mcq.explanation = regenerated_explanations
                            mcq.explanation_sections = None
                            mcq.save(update_fields=["unified_explanation", "explanation", "explanation_sections"])
                            logger.info(f"Auto-regenerated explanations for MCQ #{mcq_id} after AI options update")

                            from .explanation_utils import render_explanation_as_html

                            response_data['unified_explanation'] = regenerated_explanations
                            response_data['html_preview'] = render_explanation_as_html(regenerated_explanations)
                            response_data['explanations_regenerated'] = True
                            response_data['message'] += ', applied changes, and regenerated explanations'
                        else:
                            logger.warning(f"Failed to regenerate explanations for MCQ #{mcq_id} - invalid response")
                            regeneration_error = "AI did not return a valid explanation"
                            response_data['message'] += ' and applied changes (explanation regeneration failed)'
                    except Exception as regen_e:
                        error_msg = str(regen_e)
                        logger.error(f"Error regenerating explanations: {error_msg}")
                        regeneration_error = f"Regeneration error: {error_msg}"
                        response_data['message'] += ' and applied changes (explanation regeneration failed)'
                else:
                    response_data['message'] += ' and applied changes'
                
                if regeneration_error:
                    response_data['regeneration_error'] = regeneration_error
                    response_data['explanations_regenerated'] = False
                    
                response_data['auto_applied'] = True
                
            except Exception as e:
                error_msg = str(e)
                logger.error(f"Error auto-applying AI options: {error_msg}")
                response_data['error'] = f"Failed to apply changes: {error_msg}"
                response_data['auto_applied'] = False
        
        logger.info(f"AI edit options successful for MCQ #{mcq_id}")
        return JsonResponse(response_data)
        
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error in ai_edit_mcq_options: {e}")
        return JsonResponse({'error': 'Invalid JSON data'}, status=400)
    except ValueError as e:
        logger.error(f"Value error in ai_edit_mcq_options for MCQ #{mcq_id}: {e}")
        return JsonResponse({'success': False, 'error': str(e)})
    except Exception as e:
        logger.error(f"Unexpected error in ai_edit_mcq_options for MCQ #{mcq_id}: {e}", exc_info=True)
        return JsonResponse({'error': f'Internal server error: {str(e)}'}, status=500)


@login_required
@staff_member_required
@require_POST
@csrf_exempt
def ai_edit_mcq_explanation(request, mcq_id):
    """Use AI to enhance MCQ explanation section"""
    import logging
    logger = logging.getLogger(__name__)
    
    mcq = get_object_or_404(MCQ, id=mcq_id)
    
    try:
        data = json.loads(request.body)
        section_name = data.get('section_name', '').strip() or 'unified_explanation'
        current_content = data.get('current_content', '').strip()
        custom_instructions = data.get('custom_instructions', '').strip()

        logger.info(
            "AI edit explanation request for MCQ #%s (instruction length: %s)",
            mcq_id,
            len(custom_instructions),
        )

        # Import AI function
        from .openai_integration import ai_edit_explanation_text, api_key, client

        # Check if OpenAI is available
        if not api_key or not client:
            return JsonResponse({
                'success': False,
                'error': 'OpenAI API is not configured. Please set the OPENAI_API_KEY environment variable.'
            }, status=503)

        enhanced_content = ai_edit_explanation_text(
            mcq,
            current_content,
            custom_instructions
        )

        if not isinstance(enhanced_content, str) or not enhanced_content.strip():
            logger.warning("AI returned empty content when enhancing explanation for MCQ #%s", mcq_id)
            return JsonResponse({
                'success': False,
                'error': 'AI returned empty content for this explanation.',
                'section_name': section_name,
            })

        logger.info("AI edit explanation successful for MCQ #%s", mcq_id)
        return JsonResponse({
            'success': True,
            'enhanced_content': enhanced_content.strip(),
            'section_name': section_name,
            'message': 'AI has enhanced the explanation.',
        })
        
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error in ai_edit_mcq_explanation: {e}")
        return JsonResponse({'error': 'Invalid JSON data'}, status=400)
    except ValueError as e:
        logger.error(f"Value error in ai_edit_mcq_explanation for MCQ #{mcq_id}, section: {section_name}: {e}")
        return JsonResponse({'success': False, 'error': str(e)})
    except Exception as e:
        logger.error(f"Unexpected error in ai_edit_mcq_explanation for MCQ #{mcq_id}, section: {section_name}: {e}", exc_info=True)
        return JsonResponse({'error': f'Internal server error: {str(e)}'}, status=500)

@login_required
@staff_member_required
@require_POST
@csrf_exempt
def regenerate_all_explanations(request, mcq_id):
    """Regenerate all 9 explanation sections for an MCQ using AI"""
    mcq = get_object_or_404(MCQ, id=mcq_id)
    
    try:
        payload = {}
        if request.body:
            try:
                payload = json.loads(request.body)
            except json.JSONDecodeError:
                logger.warning("Invalid JSON payload received for regenerate_all_explanations; proceeding without custom instructions")
                payload = {}
        custom_instructions = payload.get('custom_instructions', '').strip()
        if custom_instructions:
            logger.info(f"Custom instructions provided for MCQ #{mcq_id} explanation regeneration ({len(custom_instructions)} chars)")

        # Import AI function
        from .openai_integration import regenerate_unified_explanation
        from .explanation_utils import render_explanation_as_html

        regenerated_text = regenerate_unified_explanation(
            mcq,
            custom_instructions=custom_instructions,
        )

        if regenerated_text and regenerated_text.strip():
            regenerated_text = regenerated_text.strip()
            mcq.unified_explanation = regenerated_text
            mcq.explanation = regenerated_text
            mcq.explanation_sections = None
            mcq.save(update_fields=["unified_explanation", "explanation", "explanation_sections"])

            return JsonResponse({
                'success': True,
                'message': 'Explanation regenerated successfully',
                'unified_explanation': regenerated_text,
                'html_preview': render_explanation_as_html(regenerated_text),
            })
        return JsonResponse({
            'success': False,
            'error': 'Failed to regenerate the explanation. OpenAI API may be unavailable.'
        })

    except ValueError as e:
        logger.error(f"Validation error regenerating explanations for MCQ #{mcq_id}: {str(e)}")
        return JsonResponse({
            'success': False,
            'error': str(e)
        })
    except Exception as e:
        logger.error(f"Error regenerating all explanations for MCQ #{mcq_id}: {str(e)}")
        return JsonResponse({
            'success': False,
            'error': f'Failed to regenerate explanations: {str(e)}'
        }, status=500)


@login_required
@require_POST
@csrf_exempt
def mcq_to_case_learning(request, mcq_id):
    """Convert MCQ to interactive case-based learning session using background task"""
    from .models import MCQCaseConversionSession
    from .tasks import process_mcq_to_case_conversion
    from .case_conversion_tracker import conversion_tracker
    
    mcq = get_object_or_404(MCQ, id=mcq_id)
    
    # Start comprehensive tracking
    tracking_id = conversion_tracker.start_conversion_tracking(
        mcq_id=mcq_id, 
        user_id=request.user.id, 
        request_source="frontend_button_click"
    )
    
    try:
        # Check if there's an existing ready conversion for this user and MCQ
        existing_session = MCQCaseConversionSession.objects.filter(
            mcq=mcq,
            user=request.user,
            status=MCQCaseConversionSession.READY
        ).order_by('-created_at').first()
        
        if existing_session:
            # Log existing session usage
            conversion_tracker.log_django_session_transfer(
                tracking_id=tracking_id,
                django_session_key="existing_session_reused",
                transfer_checksum="reused",
                case_data_summary={
                    'source_mcq_id': existing_session.case_data.get('source_mcq_id'),
                    'has_clinical_presentation': bool(existing_session.case_data.get('clinical_presentation'))
                }
            )
            
            # Transfer case data to Django session with integrity protection
            from .end_to_end_integrity import e2e_integrity
            success, case_session_id, error = e2e_integrity.transfer_to_django_session(request, existing_session)
            
            if not success:
                conversion_tracker.log_error(tracking_id, "EXISTING_SESSION_TRANSFER", error)
                return JsonResponse({
                    'success': False,
                    'error': f'Session transfer failed: {error}'
                }, status=500)
            
            # Log successful transfer
            conversion_tracker.log_django_session_transfer(
                tracking_id=tracking_id,
                django_session_key=case_session_id,
                transfer_checksum="existing_session",
                case_data_summary={
                    'source_mcq_id': existing_session.case_data.get('source_mcq_id'),
                    'has_clinical_presentation': bool(existing_session.case_data.get('clinical_presentation'))
                }
            )
            
            # Prepare response
            response_data = {
                'success': True,
                'status': 'ready',
                'session_id': existing_session.id,
                'session_key': case_session_id,
                'case_data': existing_session.case_data,
                'message': 'Using existing case conversion',
                'tracking_id': tracking_id  # Include tracking ID for debugging
            }
            
            # Log frontend response
            conversion_tracker.log_frontend_response(tracking_id, response_data)
            
            # Use the existing conversion
            return JsonResponse(response_data)
        
        # Check if there's a processing session
        processing_session = MCQCaseConversionSession.objects.filter(
            mcq=mcq,
            user=request.user,
            status=MCQCaseConversionSession.PROCESSING
        ).first()
        
        if processing_session:
            # Return processing status
            return JsonResponse({
                'success': True,
                'status': 'processing',
                'session_id': processing_session.id,
                'message': 'Case conversion is already in progress'
            })
        
        # Create new conversion session with end-to-end integrity
        from .end_to_end_integrity import e2e_integrity
        session = e2e_integrity.create_secure_conversion_session(mcq, request.user)
        
        # Log session creation
        conversion_tracker.log_conversion_session_creation(
            tracking_id=tracking_id,
            session_id=session.id,
            session_fingerprint=session.case_data.get('_integrity_metadata', {}).get('session_fingerprint', 'unknown'),
            mcq_content_hash=session.case_data.get('_integrity_metadata', {}).get('mcq_content_hash', 'unknown')
        )
        
        # Launch background task
        task = process_mcq_to_case_conversion.delay(mcq_id, request.user.id, tracking_id)
        
        # Log background task start
        conversion_tracker.log_background_task_start(
            tracking_id=tracking_id,
            task_id=task.id,
            mcq_id=mcq_id,
            user_id=request.user.id
        )
        
        # Update session with task ID
        session.task_id = task.id
        session.status = MCQCaseConversionSession.PROCESSING
        session.save()
        
        # Prepare response
        response_data = {
            'success': True,
            'status': 'processing',
            'session_id': session.id,
            'task_id': task.id,
            'message': 'Case conversion started. Please wait...',
            'tracking_id': tracking_id  # Include tracking ID for debugging
        }
        
        # Log frontend response
        conversion_tracker.log_frontend_response(tracking_id, response_data)
        
        return JsonResponse(response_data)
        
    except Exception as e:
        print(f'Error initiating MCQ {mcq_id} conversion: {e}')
        return JsonResponse({
            'success': False,
            'error': f'Failed to start case conversion: {str(e)}'
        }, status=500)


@login_required
def check_mcq_conversion_status(request, session_id):
    """Check the status of MCQ case conversion"""
    from .models import MCQCaseConversionSession
    
    try:
        session = get_object_or_404(
            MCQCaseConversionSession, 
            id=session_id,
            user=request.user
        )
        
        response_data = {
            'status': session.status,
            'session_id': session.id,
            'mcq_id': session.mcq.id
        }
        
        if session.status == MCQCaseConversionSession.READY:
            # Transfer case data to Django session with integrity protection
            from .end_to_end_integrity import e2e_integrity
            success, case_session_id, error = e2e_integrity.transfer_to_django_session(request, session)
            
            if not success:
                return JsonResponse({
                    'success': False,
                    'error': f'Session transfer failed: {error}'
                }, status=500)
            
            response_data.update({
                'case_data': session.case_data,
                'session_key': case_session_id,
                'message': 'Case conversion completed successfully'
            })
        elif session.status == MCQCaseConversionSession.FAILED:
            # Extract debug log if present in error message
            debug_log = None
            error_message = session.error_message or 'Conversion failed'
            
            if '\n\nDebug Log:\n' in error_message:
                parts = error_message.split('\n\nDebug Log:\n', 1)
                error_message = parts[0]
                try:
                    import json
                    debug_log = json.loads(parts[1])
                except:
                    debug_log = parts[1]  # Raw text if JSON parsing fails
            
            response_data.update({
                'error': error_message,
                'message': 'Case conversion failed',
                'debug_log': debug_log
            })
        else:
            response_data['message'] = 'Case conversion in progress...'
        
        return JsonResponse(response_data)
        
    except Exception as e:
        return JsonResponse({
            'status': 'error',
            'error': str(e)
        }, status=500)


# ===== ADMIN DEBUG CONSOLE FOR MCQ SESSION TRACKING =====

@staff_member_required
def admin_debug_console(request):
    """
    🔧 Admin Debug Console for MCQ Case Conversion Session Tracking
    Comprehensive debugging interface for tracking session mismatches
    and unified activity logging across frontend and backend flows.
    """
    from django.contrib.sessions.models import Session

    now = timezone.now()
    record_admin_debug_event(
        "console_opened",
        "Admin Debug Console opened",
        request=request,
        source="backend",
        severity="info",
    )

    recent_sessions_qs = (
        MCQCaseConversionSession.objects.select_related("user", "mcq")
        .filter(created_at__gte=now - timedelta(hours=24))
        .order_by("-created_at")
    )

    # Django session snapshot
    django_sessions: list[dict[str, object]] = []
    for django_session in Session.objects.filter(expire_date__gte=now).order_by("-expire_date")[:50]:
        try:
            session_data = django_session.get_decoded()
        except Exception:
            continue
        case_keys = [key for key in session_data.keys() if key.startswith("case_session_")]
        for case_key in case_keys:
            case_data = session_data.get(case_key, {}) or {}
            django_sessions.append(
                {
                    "session_key": f"{django_session.session_key[:10]}...",
                    "case_key": case_key,
                    "mcq_id": case_data.get("mcq_id"),
                    "user_id": case_data.get("user_id"),
                    "created_at": case_data.get("created_at"),
                    "expire_date": django_session.expire_date,
                }
            )

    # Conversion session analysis
    session_analysis = []
    session_mismatches = []
    integrity_scores = []
    recent_sessions_table = []

    for session in recent_sessions_qs[:50]:
        analysis = {
            "conversion_session": session,
            "status": session.status,
            "has_mismatch": False,
            "mismatch_details": [],
            "case_source_mcq_id": None,
            "integrity_score": 100,
        }

        case_data = session.case_data or {}
        case_source_id = case_data.get("source_mcq_id")
        analysis["case_source_mcq_id"] = case_source_id

        if case_source_id is not None and case_source_id != session.mcq_id:
            analysis["has_mismatch"] = True
            analysis["mismatch_details"].append(
                f"MCQ ID mismatch: Session={session.mcq_id}, Case={case_source_id}"
            )
            analysis["integrity_score"] -= 50

        demographics = (case_data.get("patient_demographics") or "").lower()
        if demographics and "unknown" in demographics:
            analysis["mismatch_details"].append("Patient demographics contain 'unknown'")
            analysis["integrity_score"] -= 10

        validation = case_data.get("professional_validation") or {}
        validation_score = validation.get("score", 0)
        if validation_score < 80:
            analysis["mismatch_details"].append(f"Low validation score: {validation_score}")
            analysis["integrity_score"] -= 20

        if analysis["has_mismatch"]:
            session_mismatches.append(
                {
                    "id": session.id,
                    "mcq_id": session.mcq_id,
                    "case_mcq_id": case_source_id,
                    "user_username": getattr(session.user, "username", "Unknown"),
                    "created_at": session.created_at,
                    "status": session.status.upper(),
                }
            )

        session_analysis.append(analysis)
        integrity_scores.append(analysis["integrity_score"])

        recent_sessions_table.append(
            {
                "id": session.id,
                "mcq_id": session.mcq_id,
                "user_username": getattr(session.user, "username", "Unknown"),
                "subspecialty": getattr(session.mcq, "subspecialty", "Unknown"),
                "created_at": session.created_at,
                "validation_score": validation_score,
                "status": session.status.upper(),
            }
        )

    overall_integrity_score = round(
        sum(integrity_scores) / len(integrity_scores), 1
    ) if integrity_scores else 100.0

    # Unified event log
    initial_events_qs = AdminDebugEvent.objects.select_related("user").order_by("-occurred_at", "-id")[:200]
    initial_events_list = list(serialize_events(reversed(list(initial_events_qs))))
    initial_event_cursor = initial_events_list[-1]["id"] if initial_events_list else None

    context = {
        "session_analysis": session_analysis,
        "django_sessions": django_sessions[:10],
        "mismatch_count": len(session_mismatches),
        "session_mismatches": session_mismatches,
        "recent_sessions": recent_sessions_table[:15],
        "recent_successful_conversions": len(recent_sessions_table),
        "total_sessions": recent_sessions_qs.count(),
        "total_django_sessions": len(django_sessions),
        "overall_integrity_score": overall_integrity_score,
        "debug_timestamp": now,
        "initial_event_log": initial_events_list,
        "initial_event_cursor": initial_event_cursor,
        "total_event_count": AdminDebugEvent.objects.count(),
        "session_key": getattr(request.session, "session_key", "") or "",
    }

    return render(request, "mcq/admin_debug_console.html", context)


@staff_member_required
def admin_debug_events_api(request):
    """
    Fetch admin debug events newer than the provided cursor.
    """
    try:
        since = request.GET.get("since")
        limit = request.GET.get("limit")
        queryset = AdminDebugEvent.objects.select_related("user")
        if since:
            try:
                since_id = int(since)
            except ValueError:
                return JsonResponse({"error": "Invalid 'since' cursor."}, status=400)
            queryset = queryset.filter(id__gt=since_id)

        limit_value = 200
        if limit:
            try:
                limit_value = max(1, min(int(limit), 500))
            except ValueError:
                pass

        events = list(serialize_events(queryset.order_by("id")[:limit_value]))
        latest = events[-1]["id"] if events else None

        return JsonResponse(
            {
                "events": events,
                "latest": latest,
                "total_events": AdminDebugEvent.objects.count(),
            }
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Failed to fetch admin debug events: %s", exc)
        return JsonResponse({"error": str(exc)}, status=500)


@staff_member_required
@csrf_exempt
def admin_log_client_event(request):
    """
    Receive batched frontend instrumentation events from the Admin Debug Console.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    events = payload.get("events", [])
    if not isinstance(events, list):
        return JsonResponse({"error": "Events payload must be a list."}, status=400)

    saved_events = []
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = event.get("event_type", "frontend_event")
        message = event.get("message", "").strip() or event_type.replace("_", " ").title()
        severity = event.get("severity", "info")
        source = event.get("source", "frontend")
        occurred_at = event.get("timestamp")
        payload_data = event.get("payload") or {}
        session_key = event.get("session_key", "")

        saved_events.append(
            record_admin_debug_event(
                event_type,
                message,
                severity=severity,
                source=source,
                request=request,
                payload=payload_data,
                session_key=session_key,
                occurred_at=occurred_at,
            )
        )

    latest_id = saved_events[-1].id if saved_events else None
    return JsonResponse({"success": True, "saved": len(saved_events), "latest": latest_id})


@staff_member_required
@csrf_exempt
def debug_session_integrity(request):
    """
    API endpoint for checking specific session integrity
    """
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            session_id = data.get('session_id')
            
            if not session_id:
                return JsonResponse({'error': 'Session ID required'}, status=400)
            
            # Check conversion session
            try:
                conversion_session = MCQCaseConversionSession.objects.get(id=session_id)
            except MCQCaseConversionSession.DoesNotExist:
                return JsonResponse({'error': 'Session not found'}, status=404)
            
            # Perform comprehensive integrity check
            integrity_report = {
                'session_id': session_id,
                'mcq_id': conversion_session.mcq_id,
                'user_id': conversion_session.user_id,
                'status': conversion_session.status,
                'created_at': conversion_session.created_at.isoformat(),
                'checks': []
            }
            
            # Check 1: Basic data consistency
            if conversion_session.case_data:
                case_source_id = conversion_session.case_data.get('source_mcq_id')
                if case_source_id == conversion_session.mcq_id:
                    integrity_report['checks'].append({
                        'name': 'MCQ ID Consistency',
                        'status': 'PASS',
                        'details': f'Session MCQ ID ({conversion_session.mcq_id}) matches case source ID ({case_source_id})'
                    })
                else:
                    integrity_report['checks'].append({
                        'name': 'MCQ ID Consistency',
                        'status': 'FAIL',
                        'details': f'MISMATCH: Session MCQ ID ({conversion_session.mcq_id}) != Case source ID ({case_source_id})'
                    })
                
                # Check 2: Case validation scores
                validation = conversion_session.case_data.get('professional_validation', {})
                score = validation.get('score', 0)
                if score >= 80:
                    integrity_report['checks'].append({
                        'name': 'Validation Score',
                        'status': 'PASS',
                        'details': f'High validation score: {score}/100'
                    })
                else:
                    integrity_report['checks'].append({
                        'name': 'Validation Score',
                        'status': 'WARN',
                        'details': f'Low validation score: {score}/100'
                    })
                
                # Check 3: Patient demographics quality
                demographics = conversion_session.case_data.get('patient_demographics', '')
                if demographics and 'unknown' not in demographics.lower():
                    integrity_report['checks'].append({
                        'name': 'Patient Demographics',
                        'status': 'PASS',
                        'details': f'Demographics specified: {demographics}'
                    })
                else:
                    integrity_report['checks'].append({
                        'name': 'Patient Demographics',
                        'status': 'WARN',
                        'details': f'Demographics contain unknown or missing: {demographics}'
                    })
                
                # Check 4: Case content length and quality
                clinical_presentation = conversion_session.case_data.get('clinical_presentation', '')
                if len(clinical_presentation) > 100:
                    integrity_report['checks'].append({
                        'name': 'Clinical Presentation',
                        'status': 'PASS',
                        'details': f'Substantial clinical presentation ({len(clinical_presentation)} characters)'
                    })
                else:
                    integrity_report['checks'].append({
                        'name': 'Clinical Presentation',
                        'status': 'WARN',
                        'details': f'Brief clinical presentation ({len(clinical_presentation)} characters)'
                    })
            else:
                integrity_report['checks'].append({
                    'name': 'Case Data Existence',
                    'status': 'FAIL',
                    'details': 'No case data found in session'
                })
            
            # Check 5: Django session consistency
            django_session_key = f"case_session_mcq_{conversion_session.mcq_id}_{conversion_session.id}_{conversion_session.user_id}"
            django_case_data = request.session.get(django_session_key)
            
            if django_case_data:
                django_mcq_id = django_case_data.get('mcq_id')
                if django_mcq_id == conversion_session.mcq_id:
                    integrity_report['checks'].append({
                        'name': 'Django Session Consistency',
                        'status': 'PASS',
                        'details': f'Django session MCQ ID matches: {django_mcq_id}'
                    })
                else:
                    integrity_report['checks'].append({
                        'name': 'Django Session Consistency',
                        'status': 'FAIL',
                        'details': f'Django session MCQ ID mismatch: {django_mcq_id} != {conversion_session.mcq_id}'
                    })
            else:
                integrity_report['checks'].append({
                    'name': 'Django Session Existence',
                    'status': 'WARN',
                    'details': f'Django session not found for key: {django_session_key}'
                })
            
            # Calculate overall integrity score
            total_checks = len(integrity_report['checks'])
            passed_checks = len([c for c in integrity_report['checks'] if c['status'] == 'PASS'])
            integrity_report['integrity_score'] = (passed_checks / total_checks * 100) if total_checks > 0 else 0

            record_admin_debug_event(
                "backend_integrity_report",
                f"Integrity check executed for session {session_id}",
                severity="success",
                source="backend",
                request=request,
                payload=integrity_report,
            )

            return JsonResponse(integrity_report)

        except json.JSONDecodeError:
            record_admin_debug_event(
                "backend_integrity_report_error",
                "Integrity check failed: invalid JSON payload",
                severity="error",
                source="backend",
                request=request,
                payload={'body': request.body.decode('utf-8', errors='ignore')},
            )
            return JsonResponse({'error': 'Invalid JSON'}, status=400)
        except Exception as e:
            record_admin_debug_event(
                "backend_integrity_report_error",
                f"Integrity check failed for session {request.body}: {e}",
                severity="error",
                source="backend",
                request=request,
                payload={'error': str(e)},
            )
            return JsonResponse({'error': str(e)}, status=500)
    
    return JsonResponse({'error': 'Method not allowed'}, status=405)


@staff_member_required
@csrf_exempt  
def debug_clear_session_cache(request):
    """
    Clear MCQ case conversion cache and sessions for debugging
    """
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            action = data.get('action', 'cache_only')
            
            cleared_items = {'cache_keys': 0, 'django_sessions': 0, 'conversion_sessions': 0}
            
            if action in ['cache_only', 'all']:
                # Clear MCQ conversion cache
                from django.core.cache import cache
                cache_patterns = ['mcq_case_conversion_*', 'django_session_verification_*']
                # Note: Django cache doesn't support pattern deletion, so we'll clear all
                cache.clear()
                cleared_items['cache_keys'] = 1  # Symbolic count
            
            if action in ['sessions_only', 'all']:
                # Clear Django sessions with case data
                from django.contrib.sessions.models import Session
                for django_session in Session.objects.all():
                    try:
                        session_data = django_session.get_decoded()
                        case_keys = [key for key in session_data.keys() if key.startswith('case_session_')]
                        if case_keys:
                            for case_key in case_keys:
                                del session_data[case_key]
                            django_session.session_data = django_session.encode(session_data)
                            django_session.save()
                            cleared_items['django_sessions'] += 1
                    except Exception:
                        continue
            
            if action in ['conversions_only', 'all']:
                # Clear old conversion sessions
                old_sessions = MCQCaseConversionSession.objects.filter(
                    created_at__lt=timezone.now() - timedelta(hours=24)
                )
                cleared_items['conversion_sessions'] = old_sessions.count()
                old_sessions.delete()
            
            response_payload = {
                'success': True,
                'message': 'Cache clearing completed',
                'cleared': cleared_items
            }
            record_admin_debug_event(
                "backend_cache_clear",
                f"Cache clear executed ({action})",
                severity="success",
                source="backend",
                request=request,
                payload=response_payload,
            )
            return JsonResponse(response_payload)
            
        except json.JSONDecodeError:
            record_admin_debug_event(
                "backend_cache_clear_error",
                "Cache clear failed: invalid JSON payload",
                severity="error",
                source="backend",
                request=request,
            )
            return JsonResponse({'error': 'Invalid JSON'}, status=400)
        except Exception as e:
            record_admin_debug_event(
                "backend_cache_clear_error",
                f"Cache clear failed: {e}",
                severity="error",
                source="backend",
                request=request,
                payload={'error': str(e)},
            )
            return JsonResponse({'error': str(e)}, status=500)
    
    return JsonResponse({'error': 'Method not allowed'}, status=405)


@staff_member_required
def debug_trace_mcq_conversion(request, mcq_id):
    """
    Trace the complete conversion process for a specific MCQ
    """
    try:
        mcq = get_object_or_404(MCQ, id=mcq_id)
        
        # Get all conversion sessions for this MCQ
        conversion_sessions = MCQCaseConversionSession.objects.filter(
            mcq_id=mcq_id
        ).order_by('-created_at')[:10]
        
        # Trace Django sessions
        django_sessions = []
        from django.contrib.sessions.models import Session
        for django_session in Session.objects.filter(expire_date__gte=timezone.now()):
            try:
                session_data = django_session.get_decoded()
                for key, value in session_data.items():
                    if key.startswith('case_session_mcq_') and str(mcq_id) in key:
                        if isinstance(value, dict) and value.get('mcq_id') == mcq_id:
                            django_sessions.append({
                                'session_key': django_session.session_key,
                                'case_key': key,
                                'data': value,
                                'expire_date': django_session.expire_date
                            })
            except Exception:
                continue
        
        trace_data = {
            'mcq': {
                'id': mcq.id,
                'subspecialty': mcq.subspecialty,
                'question_preview': mcq.question_text[:200] + '...' if len(mcq.question_text) > 200 else mcq.question_text
            },
            'conversion_sessions': [],
            'django_sessions': django_sessions,
            'trace_timestamp': timezone.now().isoformat()
        }
        
        for session in conversion_sessions:
            session_info = {
                'id': session.id,
                'user_id': session.user_id,
                'status': session.status,
                'created_at': session.created_at.isoformat(),
                'has_case_data': bool(session.case_data),
                'integrity_issues': [],
                'validation_details': None,
                'case_preview': None,
                'error_message': session.error_message
            }
            
            case_data = session.case_data or {}

            if isinstance(case_data, str):
                try:
                    case_data = json.loads(case_data)
                except json.JSONDecodeError:
                    session_info['integrity_issues'].append('Case data stored as plain string and is not valid JSON')
                    case_data = {}

            if case_data and not isinstance(case_data, dict):
                session_info['integrity_issues'].append(f"Case data has unexpected type: {type(case_data).__name__}")
                case_data = {}

            if case_data:
                case_source_id = case_data.get('source_mcq_id')
                if case_source_id != mcq_id:
                    session_info['integrity_issues'].append(f"MCQ ID mismatch: {case_source_id} != {mcq_id}")
                
                # Extract detailed validation information
                validation = case_data.get('professional_validation', {})
                session_info['validation_details'] = {
                    'passed': validation.get('passed', False),
                    'score': validation.get('score', 0),
                    'reason': validation.get('reason', 'No reason provided'),
                    'method': validation.get('method', 'unknown'),
                    'issues': validation.get('issues', []),
                    'has_warnings': validation.get('has_warnings', False),
                    'warning_count': validation.get('warning_count', 0),
                    'critical_issues': validation.get('critical_issues', [])
                }
                
                # Get case preview
                clinical_presentation = case_data.get('clinical_presentation', '')
                if clinical_presentation:
                    session_info['case_preview'] = clinical_presentation[:300] + '...' if len(clinical_presentation) > 300 else clinical_presentation
                
                # Additional case details
                session_info['case_details'] = {
                    'patient_demographics': case_data.get('patient_demographics', 'N/A'),
                    'specialty': case_data.get('specialty', 'N/A'),
                    'core_concept': case_data.get('core_concept_type', 'N/A'),
                    'question_type': case_data.get('question_type', 'N/A'),
                    'difficulty': case_data.get('difficulty', 'N/A')
                }
                
                # Check for debug log if available
                debug_log = case_data.get('_debug_log', [])
                if isinstance(debug_log, list) and debug_log:
                    session_info['debug_log'] = debug_log[-5:]
                
                if validation.get('score', 0) < 80:
                    session_info['integrity_issues'].append(f"Low validation score: {validation.get('score', 0)}")
            else:
                session_info['integrity_issues'].append('No structured case data found for this session')
            
            trace_data['conversion_sessions'].append(session_info)
        
        record_admin_debug_event(
            "backend_trace_mcq",
            f"Trace generated for MCQ {mcq_id}",
            severity="info",
            source="backend",
            request=request,
            payload=trace_data,
        )
        return JsonResponse(trace_data)
        
    except Exception as e:
        record_admin_debug_event(
            "backend_trace_mcq_error",
            f"Trace failed for MCQ {mcq_id}: {e}",
            severity="error",
            source="backend",
            request=request,
            payload={'error': str(e)},
        )
        return JsonResponse({'error': str(e)}, status=500)


@staff_member_required 
def test_heroku_mcq_conversion(request):
    """
    🧪 Test MCQ conversion directly on Heroku with full monitoring
    This endpoint allows Claude to test real conversions and observe results
    """
    import os
    import random
    from django.core.cache import cache
    
    try:
        # Get a random MCQ
        mcq = MCQ.objects.order_by('?').first()
        if not mcq:
            return JsonResponse({'error': 'No MCQs found in database'}, status=404)
        
        # Create a test user session (use admin user for testing)
        test_user = request.user
        
        # Log environment info
        environment_info = {
            'dyno': os.environ.get('DYNO', 'local'),
            'heroku_app': os.environ.get('HEROKU_APP_NAME', 'local'),
            'worker_available': True,  # We'll test this
            'redis_available': True,   # We'll test this
            'openai_available': True   # We'll test this
        }
        
        # Test Redis connectivity
        try:
            cache.set('test_key', 'test_value', timeout=10)
            redis_test = cache.get('test_key') == 'test_value'
            cache.delete('test_key')
            environment_info['redis_available'] = redis_test
        except Exception as e:
            environment_info['redis_available'] = False
            environment_info['redis_error'] = str(e)
        
        # Test OpenAI connectivity
        try:
            from .mcq_case_converter import MCQCaseConverter
            converter = MCQCaseConverter()
            environment_info['openai_available'] = converter.openai_client is not None
        except Exception as e:
            environment_info['openai_available'] = False
            environment_info['openai_error'] = str(e)
        
        # Clear any existing sessions for this test
        MCQCaseConversionSession.objects.filter(
            mcq=mcq,
            user=test_user
        ).delete()
        
        # Start conversion monitoring
        test_data = {
            'test_id': f"heroku_test_{timezone.now().strftime('%Y%m%d_%H%M%S')}",
            'environment': environment_info,
            'mcq': {
                'id': mcq.id,
                'subspecialty': mcq.subspecialty,
                'question_text': mcq.question_text,
                'correct_answer': mcq.correct_answer,
                'options': mcq.options if hasattr(mcq, 'options') else None
            },
            'conversion_started_at': timezone.now().isoformat(),
            'status': 'initiating'
        }
        
        # Trigger the actual conversion using the same flow as the frontend
        from .tasks import process_mcq_to_case_conversion
        from .end_to_end_integrity import e2e_integrity
        
        # Create secure conversion session
        session = e2e_integrity.create_secure_conversion_session(mcq, test_user)
        test_data['session_id'] = session.id
        
        # Launch background task (same as the real flow)
        task = process_mcq_to_case_conversion.delay(mcq.id, test_user.id)
        session.task_id = task.id
        session.status = MCQCaseConversionSession.PROCESSING
        session.save()
        
        test_data['task_id'] = task.id
        test_data['status'] = 'processing'
        
        # Store test data in cache for monitoring
        cache.set(f"heroku_test_{session.id}", test_data, timeout=300)  # 5 minutes
        
        return JsonResponse({
            'success': True,
            'test_data': test_data,
            'message': 'Heroku MCQ conversion test initiated',
            'monitor_url': f'/admin/debug/monitor-test/{session.id}/',
            'session_id': session.id
        })
        
    except Exception as e:
        import traceback
        return JsonResponse({
            'success': False,
            'error': str(e),
            'traceback': traceback.format_exc(),
            'environment': environment_info if 'environment_info' in locals() else {}
        }, status=500)


@staff_member_required
def monitor_heroku_test(request, session_id):
    """
    Monitor the status of a Heroku MCQ conversion test
    """
    try:
        # Get test data from cache
        test_data = cache.get(f"heroku_test_{session_id}")
        if not test_data:
            test_data = {'status': 'unknown', 'error': 'Test data not found in cache'}
        
        # Get current session status
        try:
            session = MCQCaseConversionSession.objects.get(id=session_id)
            current_status = {
                'session_status': session.status,
                'has_case_data': bool(session.case_data),
                'error_message': session.error_message,
                'created_at': session.created_at.isoformat(),
                'updated_at': session.updated_at.isoformat() if hasattr(session, 'updated_at') else None
            }
            
            # If completed, analyze the results
            if session.status == MCQCaseConversionSession.READY and session.case_data:
                case_analysis = {
                    'source_mcq_id': session.case_data.get('source_mcq_id'),
                    'expected_mcq_id': session.mcq_id,
                    'mcq_id_match': session.case_data.get('source_mcq_id') == session.mcq_id,
                    'patient_demographics': session.case_data.get('patient_demographics'),
                    'specialty': session.case_data.get('specialty'),
                    'core_concept': session.case_data.get('core_concept_type'),
                    'validation_score': session.case_data.get('professional_validation', {}).get('score'),
                    'validation_passed': session.case_data.get('professional_validation', {}).get('passed'),
                    'debug_log_present': '_debug_log' in session.case_data,
                    'case_preview': session.case_data.get('clinical_presentation', '')[:200] + '...'
                }
                current_status['case_analysis'] = case_analysis
                
                # Check for specific issues
                issues = []
                if not case_analysis['mcq_id_match']:
                    issues.append(f"MCQ ID mismatch: expected {session.mcq_id}, got {case_analysis['source_mcq_id']}")
                
                if case_analysis['validation_score'] and case_analysis['validation_score'] < 70:
                    issues.append(f"Low validation score: {case_analysis['validation_score']}")
                
                current_status['detected_issues'] = issues
                current_status['test_result'] = 'SUCCESS' if not issues else 'ISSUES_DETECTED'
                
            elif session.status == MCQCaseConversionSession.FAILED:
                current_status['test_result'] = 'FAILED'
                
        except MCQCaseConversionSession.DoesNotExist:
            current_status = {'error': 'Session not found'}
        
        # Update test data
        test_data['current_status'] = current_status
        test_data['last_checked'] = timezone.now().isoformat()
        
        # Cache updated data
        cache.set(f"heroku_test_{session_id}", test_data, timeout=300)
        
        return JsonResponse({
            'success': True,
            'test_data': test_data
        })
        
    except Exception as e:
        import traceback
        return JsonResponse({
            'success': False,
            'error': str(e),
            'traceback': traceback.format_exc()
        }, status=500)


@staff_member_required
def get_tracking_report(request, tracking_id):
    """Get comprehensive tracking report for MCQ-to-case conversion"""
    try:
        from .case_conversion_tracker import conversion_tracker
        
        # Get tracking report
        report = conversion_tracker.get_tracking_report(tracking_id)
        
        if not report:
            return JsonResponse({
                'success': False,
                'error': f'Tracking data not found for ID: {tracking_id}'
            }, status=404)
        
        return JsonResponse({
            'success': True,
            'tracking_id': tracking_id,
            'report': report
        })
        
    except Exception as e:
        import traceback
        return JsonResponse({
            'success': False,
            'error': str(e),
            'traceback': traceback.format_exc()
        }, status=500)


@staff_member_required
def heroku_test_interface(request):
    """
    Interface for testing MCQ conversions on Heroku
    """
    return render(request, 'mcq/heroku_test_interface.html')


@staff_member_required
def live_tracking_data(request, mcq_id):
    """Get live tracking data for a specific MCQ"""
    try:
        from .case_conversion_tracker import conversion_tracker
        from django.core.cache import cache
        
        # Look for any active tracking for this MCQ
        tracking_keys = cache.keys(f"conversion_tracking_track_{mcq_id}_*")
        
        if not tracking_keys:
            return JsonResponse({
                'success': True,
                'active_tracking': None
            })
        
        # Get the most recent tracking
        latest_key = sorted(tracking_keys)[-1]
        tracking_data = cache.get(latest_key)
        
        if tracking_data:
            # Add potential issues analysis
            tracking_data['potential_issues'] = conversion_tracker._analyze_potential_issues(tracking_data)
        
        return JsonResponse({
            'success': True,
            'active_tracking': tracking_data
        })
        
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


@staff_member_required
def export_tracking_data(request):
    """Export all tracking data as JSON"""
    try:
        from django.core.cache import cache
        import json
        
        # Get all tracking keys
        tracking_keys = cache.keys("conversion_tracking_*")
        all_tracking = []
        
        for key in tracking_keys:
            tracking_data = cache.get(key)
            if tracking_data:
                all_tracking.append(tracking_data)
        
        # Sort by start time
        all_tracking.sort(key=lambda x: x.get('start_time', ''), reverse=True)
        
        # Create JSON response
        response = HttpResponse(
            json.dumps(all_tracking, indent=2),
            content_type='application/json'
        )
        response['Content-Disposition'] = f'attachment; filename="tracking_export_{timezone.now().strftime("%Y%m%d_%H%M%S")}.json"'
        
        return response
        
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


@staff_member_required
def recent_tracking_data(request):
    """Get recent tracking data (last 24 hours)"""
    try:
        from django.core.cache import cache
        from datetime import timedelta, datetime
        
        # Get all tracking keys
        tracking_keys = cache.keys("conversion_tracking_*")
        recent_tracking = []
        
        cutoff_time = timezone.now() - timedelta(hours=24)
        
        for key in tracking_keys:
            tracking_data = cache.get(key)
            if tracking_data:
                start_time = datetime.fromisoformat(tracking_data.get('start_time', '').replace('Z', '+00:00'))
                if start_time >= cutoff_time:
                    recent_tracking.append(tracking_data)
        
        # Sort by start time
        recent_tracking.sort(key=lambda x: x.get('start_time', ''), reverse=True)
        
        return JsonResponse({
            'success': True,
            'tracking_data': recent_tracking[:100]  # Limit to 100 most recent
        })
        
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


@staff_member_required  
def search_tracking_data(request):
    """Search tracking data by criteria"""
    try:
        from django.core.cache import cache
        from .case_conversion_tracker import conversion_tracker
        
        # Get search parameters
        mcq_id = request.GET.get('mcq_id')
        user_id = request.GET.get('user_id')
        status_filter = request.GET.get('status')
        
        # Get all tracking keys
        tracking_keys = cache.keys("conversion_tracking_*")
        filtered_tracking = []
        
        for key in tracking_keys:
            tracking_data = cache.get(key)
            if not tracking_data:
                continue
                
            # Apply filters
            if mcq_id and str(tracking_data.get('mcq_id')) != mcq_id:
                continue
            if user_id and str(tracking_data.get('user_id')) != user_id:
                continue
                
            # Determine status
            if status_filter:
                steps = tracking_data.get('steps', [])
                step_types = [s['step_type'] for s in steps]
                potential_issues = conversion_tracker._analyze_potential_issues(tracking_data)
                
                if status_filter == 'error' and not any(t.startswith('ERROR_') for t in step_types):
                    continue
                elif status_filter == 'mismatch' and not any('MCQ ID mismatch' in issue for issue in potential_issues):
                    continue
                elif status_filter == 'success' and 'CASE_BOT_RESPONSE' not in step_types:
                    continue
                elif status_filter == 'incomplete' and ('CASE_BOT_RESPONSE' in step_types or any(t.startswith('ERROR_') for t in step_types)):
                    continue
            
            # Add potential issues to tracking data
            tracking_data['potential_issues'] = conversion_tracker._analyze_potential_issues(tracking_data)
            filtered_tracking.append(tracking_data)
        
        # Sort by start time
        filtered_tracking.sort(key=lambda x: x.get('start_time', ''), reverse=True)
        
        return JsonResponse({
            'success': True,
            'tracking_data': filtered_tracking[:200]  # Limit to 200 results
        })
        
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)
