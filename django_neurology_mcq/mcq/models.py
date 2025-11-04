"""
Models for the MCQ application.
Optimized models for better performance and maintainability.
"""

from django.db import models
from django.contrib.auth.models import User
from django.utils.translation import gettext_lazy as _
from django.db.models.signals import post_save
from django.dispatch import receiver
import json
from django.utils import timezone
from datetime import timedelta, datetime

# Import High-yield Review models
from .high_yield_models import HighYieldSpecialty, HighYieldTopic, TopicSectionImage

class MCQ(models.Model):
    """
    Multiple-choice question model.
    Core model that stores questions with associated metadata, options, and correct answers.
    """
    
    # Choice constants for exam types
    BASIC_LEVEL = 'Basic level'
    ADVANCED = 'Advanced'
    BOARD_LEVEL = 'Board-level'
    OTHER = 'Other'
    
    # Legacy constants for backward compatibility
    PART_I = 'Advanced'
    PART_II = 'Board-level'
    PROMOTION = 'Basic level'
    
    EXAM_TYPE_CHOICES = [
        (BASIC_LEVEL, _('Basic level')),
        (ADVANCED, _('Advanced')),
        (BOARD_LEVEL, _('Board-level')),
        (OTHER, _('Other')),
    ]
    
    # Basic fields (required)
    question_number = models.CharField(
        max_length=20, 
        blank=True, 
        null=True,
        db_index=True,
        help_text=_("Unique identifier for the question, e.g., 'Q123'")
    )
    question_text = models.TextField(
        help_text=_("The full text of the question")
    )
    options = models.JSONField(
        default=list, 
        blank=True, 
        null=True,
        help_text=_("Answer options stored as JSON array, e.g., ['Option A', 'Option B', 'Option C', 'Option D']")
    )
    correct_answer = models.CharField(
        max_length=15,  # Increased to accommodate multi-answer options like "A, B, C, D"
        help_text=_("Letter(s) of the correct answer option (A, B, C, D, etc. or comma-separated for multiple answers)")
    )
    correct_answer_text = models.TextField(
        blank=True,
        null=True,
        help_text=_("Text of the correct answer option")
    )
    
    # Classification and metadata (with indexes for faster queries)
    subspecialty = models.CharField(
        max_length=100,
        db_index=True,
        help_text=_("Neurological subspecialty this question belongs to")
    )
    source_file = models.CharField(
        max_length=255, 
        blank=True, 
        null=True,
        help_text=_("Original source file or reference")
    )
    exam_type = models.CharField(
        max_length=50, 
        blank=True, 
        null=True,
        db_index=True,
        help_text=_("Type of exam this question appeared in")
    )
    exam_year = models.CharField(
        max_length=10,
        blank=True, 
        null=True,
        db_index=True,
        help_text=_("Year the question appeared in an exam")
    )
    
    # New fields from updated MCQ format
    ai_generated = models.BooleanField(
        default=False,
        help_text=_("Whether this question was AI-generated")
    )
    unified_explanation = models.TextField(
        blank=True,
        null=True,
        help_text=_("Unified explanation text for questions with single explanations")
    )
    fixed_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text=_("Timestamp when the question was last fixed or updated")
    )
    word_count = models.IntegerField(
        blank=True,
        null=True,
        help_text=_("Word count of the explanation")
    )
    
    # Additional content
    explanation = models.TextField(
        blank=True,
        null=True,
        help_text=_("Detailed explanation of the answer")
    )
    
    # Structured explanation sections
    explanation_sections = models.JSONField(
        blank=True,
        null=True,
        help_text=_("Structured explanation sections in JSON format")
    )
    
    # Additional verification and categorization fields
    verification_confidence = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        help_text=_("Confidence level of the verification")
    )
    
    primary_category = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text=_("Primary category of the question")
    )
    
    secondary_category = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text=_("Secondary category of the question")
    )
    
    key_concept = models.TextField(
        blank=True,
        null=True,
        help_text=_("Key concept tested in the question")
    )
    
    difficulty_level = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        help_text=_("Difficulty level of the question")
    )

    # Image URL for questions with images
    image_url = models.URLField(
        blank=True,
        null=True,
        help_text=_("URL to an image for this question (if applicable)")
    )

    def get_unified_explanation_text(self) -> str:
        """
        Return the preferred explanation text for the MCQ.

        Priority order:
        1. Unified explanation field
        2. Legacy explanation field
        3. Merged legacy structured sections
        """
        if self.unified_explanation:
            return self.unified_explanation
        if self.explanation:
            return self.explanation

        from .explanation_utils import merge_sections_to_text

        if self.explanation_sections:
            try:
                return merge_sections_to_text(self.explanation_sections)
            except Exception:
                pass
        return ""
    
    def save(self, *args, **kwargs):
        # Automatically convert Google Drive URLs to direct image format
        if self.image_url:
            # Handle various Google Drive URL formats
            if 'drive.google.com' in self.image_url:
                import re
                
                # Extract file ID from various Google Drive URL formats
                patterns = [
                    r'/file/d/([a-zA-Z0-9_-]+)',  # /file/d/FILE_ID/ format
                    r'/d/([a-zA-Z0-9_-]+)',  # /d/FILE_ID/ format
                    r'id=([a-zA-Z0-9_-]+)',  # ?id=FILE_ID format
                    r'open\?id=([a-zA-Z0-9_-]+)',  # open?id=FILE_ID format
                ]
                
                file_id = None
                for pattern in patterns:
                    match = re.search(pattern, self.image_url)
                    if match:
                        file_id = match.group(1)
                        break
                
                if file_id:
                    # Store as preview URL for iframe embedding
                    self.image_url = f'https://drive.google.com/file/d/{file_id}/preview'
        
        super().save(*args, **kwargs)
    
    class Meta:
        indexes = [
            models.Index(fields=['subspecialty', 'exam_type']),
            models.Index(fields=['question_number']),
        ]
        verbose_name = _("MCQ")
        verbose_name_plural = _("MCQs")
    
    def __str__(self):
        return f"{self.question_number or 'ID:'+str(self.id)}: {self.question_text[:50]}..."
    
    def get_options_dict(self):
        """
        Get options as dictionary, handling both array and dictionary formats.
        Returns a dictionary of options with letter keys.
        """
        if isinstance(self.options, str):
            try:
                parsed = json.loads(self.options)
                if isinstance(parsed, list):
                    # Convert array to dict with letter keys
                    return {chr(65 + i): option for i, option in enumerate(parsed)}
                return parsed
            except json.JSONDecodeError:
                return {}
        elif isinstance(self.options, list):
            # Convert array to dict with letter keys
            return {chr(65 + i): option for i, option in enumerate(self.options)}
        return self.options or {}
    
    def get_options_list(self):
        """
        Get options as list, handling both array and dictionary formats.
        Returns a list of options.
        """
        if isinstance(self.options, str):
            try:
                parsed = json.loads(self.options)
                if isinstance(parsed, dict):
                    # Convert dict to ordered list
                    return [parsed.get(chr(65 + i), '') for i in range(len(parsed))]
                return parsed
            except json.JSONDecodeError:
                return []
        elif isinstance(self.options, dict):
            # Convert dict to ordered list
            return [self.options.get(chr(65 + i), '') for i in range(len(self.options))]
        return self.options or []
    
    @property
    def has_explanation(self):
        """Check if this MCQ has a proper explanation."""
        # First, check for unified explanation
        if self.unified_explanation and len(self.unified_explanation.strip()) > 50:
            return True
            
        # Check for structured explanation sections
        if self.explanation_sections:
            # Check if any of the key sections have content
            required_sections = ['option_analysis', 'conceptual_foundation', 'clinical_manifestation']
            for section in required_sections:
                if section in self.explanation_sections and self.explanation_sections.get(section, ''):
                    if len(str(self.explanation_sections.get(section, '')).strip()) > 50:
                        return True
        
        # Traditional explanation field check
        if not self.explanation:
            return False
            
        # Skip if the explanation is too short (less than 50 characters)
        if len(self.explanation.strip()) < 50:
            return False
            
        # Skip if it's just a Classification Reason 
        if "Classification Reason:" in self.explanation:
            return False
            
        # Skip if it's just a basic placeholder
        placeholder_texts = [
            "No detailed explanation available",
            "No explanation available",
            "Classification:",
            "# Classification",
            "Explanation not yet generated",
            "Explanation Needed",
            "# Explanation Needed",
            "This MCQ requires a detailed explanation",
            "You can generate one using the",
            "This question has been reviewed by specialists"
        ]
        
        if any(placeholder in self.explanation for placeholder in placeholder_texts):
            # Only return False if the explanation is just the placeholder without much else
            if len(self.explanation.strip()) < 100:
                return False
        
        # Skip if it has no proper content (just headers without content)
        if self.explanation.count('#') > 0 and len(self.explanation.strip()) < 150:
            # Count how many substantial paragraphs exist
            paragraphs = [p.strip() for p in self.explanation.split('\n\n')]
            substantial_paragraphs = [p for p in paragraphs if len(p) > 30 and not p.startswith('#')]
            
            if len(substantial_paragraphs) == 0:
                return False
        
        return True
    
    def has_valid_answer(self):
        """Check if the MCQ has a valid correct answer that matches one of its options."""
        if not self.correct_answer:
            return False
        
        # Check if options exist
        if not self.options:
            return False
        
        # Check if correct_answer is in the options
        return self.correct_answer in self.options
    
    def get_answer_display(self):
        """Get a display-friendly version of the correct answer."""
        if not self.correct_answer:
            return "No answer set"
        
        if self.has_valid_answer() and self.options:
            # Return the answer letter with its text
            return f"{self.correct_answer}. {self.options.get(self.correct_answer, '')}"
        else:
            # Just return the answer letter/value
            return f"{self.correct_answer} (Invalid - not in options)"


class UserMCQInteraction(models.Model):
    """
    Abstract base class for user interactions with MCQs.
    Provides common fields and configuration for derived models.
    """
    user = models.ForeignKey(
        User, 
        on_delete=models.CASCADE,
        help_text=_("User who interacted with this MCQ")
    )
    mcq = models.ForeignKey(
        MCQ, 
        on_delete=models.CASCADE,
        help_text=_("The associated MCQ")
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text=_("When this interaction was created")
    )
    
    class Meta:
        abstract = True
        unique_together = ['user', 'mcq']


class Bookmark(UserMCQInteraction):
    """
    Records MCQs that users have bookmarked for later reference.
    Simple model inheriting from UserMCQInteraction.
    """
    
    class Meta(UserMCQInteraction.Meta):
        verbose_name = _("Bookmark")
        verbose_name_plural = _("Bookmarks")
    
    def __str__(self):
        return f"{self.user.username} - {self.mcq.question_number or self.mcq.id}"


class Flashcard(UserMCQInteraction):
    """
    Implements spaced repetition system for MCQs.
    Includes scheduling algorithm based on SuperMemo-2.
    """
    interval = models.IntegerField(
        default=1,
        help_text=_("Current interval in days between reviews")
    )
    next_review = models.DateTimeField(
        null=True, 
        blank=True,
        db_index=True,  # Indexed for efficient queries of due cards
        help_text=_("When this flashcard is due for review")
    )
    last_reviewed = models.DateTimeField(
        null=True, 
        blank=True,
        help_text=_("When this flashcard was last reviewed")
    )
    ease_factor = models.FloatField(
        default=2.5,
        help_text=_("Spaced repetition ease factor (higher means easier to remember)")
    )
    
    class Meta(UserMCQInteraction.Meta):
        verbose_name = _("Flashcard")
        verbose_name_plural = _("Flashcards")
        indexes = [
            models.Index(fields=['user', 'next_review']),  # For efficient flashcard due queries
        ]
    
    def __str__(self):
        return f"{self.user.username} - {self.mcq.question_number or self.mcq.id}"
    
    def schedule_next_review(self, quality):
        """
        Schedule the next review based on performance quality (0-5).
        Implements the SuperMemo-2 algorithm for spaced repetition.
        
        Args:
            quality (int): Rating of recall quality from 0 (complete blackout) to 5 (perfect recall)
        """
        # Update ease factor based on performance
        self.ease_factor = max(1.3, self.ease_factor + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02)))
        
        # If quality < 3 (user didn't remember), reset interval to 1
        if quality < 3:
            self.interval = 1
        elif self.interval == 1:
            self.interval = 6  # First successful review: set to 6 days
        else:
            self.interval = round(self.interval * self.ease_factor)
        
        self.last_reviewed = timezone.now()
        self.next_review = timezone.now() + timezone.timedelta(days=self.interval)
        self.save()


class Note(UserMCQInteraction):
    """
    User's personal notes on specific MCQs.
    Adds note content and update tracking.
    """
    note_text = models.TextField(
        help_text=_("User's notes about this MCQ")
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text=_("When this note was last updated")
    )
    
    class Meta(UserMCQInteraction.Meta):
        verbose_name = _("Note")
        verbose_name_plural = _("Notes")
    
    def __str__(self):
        return f"{self.user.username} - {self.mcq.question_number or self.mcq.id}"


class ReasoningSession(models.Model):
    """
    Records a clinical reasoning session for an MCQ.
    Stores user reasoning and AI-generated guidance.
    """
    # Satisfaction level choices
    VERY_HELPFUL = 'very_helpful'
    SOMEWHAT_HELPFUL = 'somewhat_helpful'
    NOT_HELPFUL = 'not_helpful'
    
    SATISFACTION_CHOICES = [
        (VERY_HELPFUL, _('Very Helpful')),
        (SOMEWHAT_HELPFUL, _('Somewhat Helpful')),
        (NOT_HELPFUL, _('Not Helpful')),
    ]
    
    user = models.ForeignKey(
        User, 
        on_delete=models.CASCADE,
        help_text=_("User who participated in this reasoning session")
    )
    mcq = models.ForeignKey(
        MCQ, 
        on_delete=models.CASCADE,
        help_text=_("The MCQ being analyzed")
    )
    selected_answer = models.CharField(
        max_length=5,
        help_text=_("The answer option the user selected")
    )
    is_correct = models.BooleanField(
        default=False,
        help_text=_("Whether the selected answer was correct")
    )
    user_reasoning = models.TextField(
        help_text=_("User's explanation of their reasoning process")
    )
    guidance = models.TextField(
        blank=True, 
        null=True,
        help_text=_("AI-generated guidance on the clinical reasoning")
    )
    satisfaction_level = models.CharField(
        max_length=20,  # Optimized from 50 to 20 since choices are short strings
        choices=SATISFACTION_CHOICES, 
        blank=True, 
        null=True,
        help_text=_("User's satisfaction with the AI guidance")
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text=_("When this reasoning session occurred")
    )

    class Meta:
        ordering = ['-created_at']  # Most recent first for efficient queries
        verbose_name = _("Reasoning Session")
        verbose_name_plural = _("Reasoning Sessions")
        indexes = [
            models.Index(fields=['user', 'created_at']),  # For user history
            models.Index(fields=['user', 'mcq']),  # For looking up sessions for specific MCQs
        ]

    def __str__(self):
        return f"Session {self.id} - User: {self.user.username} - MCQ: {self.mcq.id}"


class CognitiveReasoningSession(models.Model):
    """
    Enhanced reasoning session with cognitive analysis and step-by-step guidance.
    Replaces the complex async system with a simpler, more robust approach.
    """
    
    # Cognitive error types
    ANCHORING_BIAS = 'anchoring_bias'
    CONFIRMATION_BIAS = 'confirmation_bias'
    AVAILABILITY_HEURISTIC = 'availability_heuristic'
    PREMATURE_CLOSURE = 'premature_closure'
    OVERCONFIDENCE = 'overconfidence'
    KNOWLEDGE_GAP = 'knowledge_gap'
    MISCONCEPTION = 'misconception'
    
    COGNITIVE_ERROR_CHOICES = [
        (ANCHORING_BIAS, _('Anchoring Bias')),
        (CONFIRMATION_BIAS, _('Confirmation Bias')),
        (AVAILABILITY_HEURISTIC, _('Availability Heuristic')),
        (PREMATURE_CLOSURE, _('Premature Closure')),
        (OVERCONFIDENCE, _('Overconfidence Bias')),
        (KNOWLEDGE_GAP, _('Knowledge Gap')),
        (MISCONCEPTION, _('Misconception')),
    ]
    
    # Session status
    ANALYZING = 'analyzing'  # Initial request received
    PROCESSING = 'processing'  # Background task in progress
    READY = 'ready'  # Analysis complete
    ERROR = 'error'  # Analysis failed
    FAILED = 'failed'  # Background task failed
    
    STATUS_CHOICES = [
        (ANALYZING, _('Analyzing')),
        (PROCESSING, _('Processing')),
        (READY, _('Ready')),
        (ERROR, _('Error')),
        (FAILED, _('Failed')),
    ]
    
    user = models.ForeignKey(
        User, 
        on_delete=models.CASCADE,
        help_text=_("User who participated in this reasoning session")
    )
    mcq = models.ForeignKey(
        MCQ, 
        on_delete=models.CASCADE,
        help_text=_("The MCQ being analyzed")
    )
    selected_answer = models.CharField(
        max_length=5,
        help_text=_("The answer option the user selected")
    )
    is_correct = models.BooleanField(
        default=False,
        help_text=_("Whether the selected answer was correct")
    )
    user_reasoning = models.TextField(
        help_text=_("User's explanation of their reasoning process")
    )
    
    # Cognitive analysis results
    primary_error = models.CharField(
        max_length=50,
        choices=COGNITIVE_ERROR_CHOICES,
        blank=True,
        null=True,
        help_text=_("Primary cognitive error identified")
    )
    secondary_errors = models.JSONField(
        default=list,
        blank=True,
        help_text=_("Additional cognitive errors identified")
    )
    knowledge_gaps = models.JSONField(
        default=list,
        blank=True,
        help_text=_("Identified knowledge gaps")
    )
    misconceptions = models.JSONField(
        default=list,
        blank=True,
        help_text=_("Identified misconceptions")
    )
    reasoning_quality = models.CharField(
        max_length=20,
        choices=[
            ('poor', _('Poor')),
            ('fair', _('Fair')),
            ('good', _('Good')),
            ('excellent', _('Excellent')),
        ],
        default='fair',
        help_text=_("Overall quality of reasoning")
    )
    confidence_score = models.FloatField(
        default=0.0,
        help_text=_("Confidence in the analysis (0.0-1.0)")
    )
    
    # Guidance content
    guidance_steps = models.JSONField(
        default=list,
        blank=True,
        help_text=_("Step-by-step guidance content")
    )
    current_step = models.IntegerField(
        default=0,
        help_text=_("Current step in the guidance process")
    )
    
    # Session management
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=ANALYZING,
        help_text=_("Current status of the session")
    )
    task_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Celery task ID for background processing")
    )
    error_message = models.TextField(
        blank=True,
        null=True,
        help_text=_("Error message if analysis failed")
    )
    
    # Feedback
    user_feedback = models.CharField(
        max_length=20,
        choices=[
            ('helpful', _('Helpful')),
            ('somewhat_helpful', _('Somewhat Helpful')),
            ('not_helpful', _('Not Helpful')),
        ],
        blank=True,
        null=True,
        help_text=_("User's feedback on the guidance")
    )
    feedback_comments = models.TextField(
        blank=True,
        null=True,
        help_text=_("Additional user feedback comments")
    )
    
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text=_("When this reasoning session was created")
    )
    completed_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text=_("When the guidance was completed")
    )
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = _("Cognitive Reasoning Session")
        verbose_name_plural = _("Cognitive Reasoning Sessions")
        indexes = [
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['status', 'created_at']),
            models.Index(fields=['mcq', 'user']),
        ]
    
    def __str__(self):
        return f"Cognitive Session {self.id} - {self.user.username} - MCQ {self.mcq.id} - {self.status}"
    
    def get_current_guidance_step(self):
        """Get the current step in the guidance process"""
        if self.guidance_steps and 0 <= self.current_step < len(self.guidance_steps):
            return self.guidance_steps[self.current_step]
        return None
    
    def advance_to_next_step(self):
        """Advance to the next step in the guidance"""
        if self.current_step < len(self.guidance_steps) - 1:
            self.current_step += 1
            self.save()
            return True
        return False
    
    def is_completed(self):
        """Check if the guidance session is completed"""
        return self.current_step >= len(self.guidance_steps) - 1 or self.status == self.READY


class MCQCaseConversionSession(models.Model):
    """
    Tracks MCQ to Case-based learning conversion sessions.
    Uses background processing to avoid timeouts.
    """
    # Status choices
    PENDING = 'pending'
    PROCESSING = 'processing'
    READY = 'ready'
    FAILED = 'failed'
    
    STATUS_CHOICES = [
        (PENDING, _('Pending')),
        (PROCESSING, _('Processing')),
        (READY, _('Ready')),
        (FAILED, _('Failed')),
    ]
    
    # Core fields
    mcq = models.ForeignKey(
        MCQ,
        on_delete=models.CASCADE,
        related_name='case_conversions',
        help_text=_("The MCQ being converted")
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='mcq_case_conversions',
        help_text=_("User who requested the conversion")
    )
    
    # Case data
    case_data = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Generated case data")
    )
    
    # Session management
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=PENDING,
        help_text=_("Current status of the conversion")
    )
    task_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Celery task ID for background processing")
    )
    error_message = models.TextField(
        blank=True,
        null=True,
        help_text=_("Error message if conversion failed")
    )
    
    # Timestamps
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text=_("When the conversion was requested")
    )
    completed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("When the conversion was completed")
    )
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = _("MCQ Case Conversion Session")
        verbose_name_plural = _("MCQ Case Conversion Sessions")
        indexes = [
            models.Index(fields=['user', 'mcq']),
            models.Index(fields=['status']),
        ]
    
    def __str__(self):
        return f"Case conversion for MCQ {self.mcq.id} by {self.user.username} ({self.status})"
    
    def is_ready(self):
        """Check if the conversion is ready"""
        return self.status == self.READY and self.case_data
    
    def get_case_url(self):
        """Get the URL to start the case session"""
        if self.is_ready() and self.case_data:
            from django.urls import reverse
            return reverse('resume_case_session', kwargs={'case_id': f"mcq_{self.mcq.id}"})
        return None


class AdminDebugEvent(models.Model):
    """
    Central event log for the Admin Debug Console.
    Captures frontend instrumentation, backend actions, API calls, and errors.
    """
    SOURCE_CHOICES = [
        ('frontend', _('Frontend')),
        ('backend', _('Backend')),
        ('api', _('API Gateway')),
        ('system', _('System')),
    ]

    SEVERITY_CHOICES = [
        ('info', _('Info')),
        ('success', _('Success')),
        ('warning', _('Warning')),
        ('error', _('Error')),
        ('critical', _('Critical')),
    ]

    created_at = models.DateTimeField(auto_now_add=True)
    occurred_at = models.DateTimeField(default=timezone.now)
    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='admin_debug_events',
        help_text=_("User associated with the event, if available."),
    )
    session_key = models.CharField(
        max_length=64,
        blank=True,
        help_text=_("Browser session key to correlate frontend events."),
    )
    event_type = models.CharField(
        max_length=64,
        help_text=_("Event type identifier (e.g., frontend_click, api_response)."),
    )
    source = models.CharField(
        max_length=16,
        choices=SOURCE_CHOICES,
        default='backend',
        help_text=_("Origin of the event."),
    )
    severity = models.CharField(
        max_length=16,
        choices=SEVERITY_CHOICES,
        default='info',
        help_text=_("Severity level for quick filtering."),
    )
    message = models.TextField(
        help_text=_("Human-readable message describing the event."),
    )
    payload = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Structured metadata for the event."),
    )

    class Meta:
        indexes = [
            models.Index(fields=['-occurred_at']),
            models.Index(fields=['event_type', '-occurred_at']),
            models.Index(fields=['source', 'severity']),
        ]
        ordering = ['-occurred_at', '-id']
        verbose_name = _("Admin Debug Event")
        verbose_name_plural = _("Admin Debug Events")

    def __str__(self) -> str:
        user_label = self.user.username if self.user else "anonymous"
        return f"[{self.source}/{self.event_type}] {self.message} ({user_label})"

    def as_dict(self):
        """Serialize for JSON responses."""
        return {
            'id': self.id,
            'event_type': self.event_type,
            'source': self.source,
            'severity': self.severity,
            'message': self.message,
            'payload': self.payload,
            'occurred_at': self.occurred_at.isoformat(),
            'created_at': self.created_at.isoformat(),
            'user': self.user.username if self.user else None,
            'session_key': self.session_key,
        }


class HiddenMCQ(models.Model):
    """
    Tracks MCQs that a user has chosen to hide from view.
    Used for personalization and to exclude content from queries.
    """
    user = models.ForeignKey(
        User, 
        on_delete=models.CASCADE,
        help_text=_("User who hid this MCQ")
    )
    mcq = models.ForeignKey(
        MCQ, 
        on_delete=models.CASCADE,
        help_text=_("The MCQ that was hidden")
    )
    hidden_at = models.DateTimeField(
        auto_now_add=True,
        help_text=_("When this MCQ was hidden")
    )
    
    class Meta:
        unique_together = ('user', 'mcq')  # Prevent duplicate entries
        verbose_name = _("Hidden MCQ")
        verbose_name_plural = _("Hidden MCQs")
        indexes = [
            models.Index(fields=['user']),  # For efficient filtering
        ]
    
    def __str__(self):
        return f"Hidden MCQ {self.mcq.id} for user {self.user.username}"


class IncorrectAnswer(UserMCQInteraction):
    """
    Records a user's incorrect answers to MCQs.
    Used for "Test My Weakness" feature to identify knowledge gaps.
    """
    selected_answer = models.CharField(
        max_length=5,
        help_text=_("The incorrect answer option the user selected")
    )
    last_tested = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("When this weakness was last tested")
    )
    resolved = models.BooleanField(
        default=False,
        help_text=_("Whether this weakness has been resolved (user got it right later)")
    )
    
    class Meta(UserMCQInteraction.Meta):
        verbose_name = _("Incorrect Answer")
        verbose_name_plural = _("Incorrect Answers")
        indexes = [
            models.Index(fields=['user', 'resolved']),  # For efficient queries of unresolved weaknesses
            models.Index(fields=['user', 'last_tested']),  # For finding least recently tested
        ]
        # Override unique_together to allow multiple wrong answers for the same MCQ
        unique_together = []
    
    def __str__(self):
        return f"{self.user.username} - {self.mcq.id} - {self.selected_answer} - {self.created_at.strftime('%Y-%m-%d')}"


class UserProfile(models.Model):
    """
    Extends the User model with additional fields for user management.
    """
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='profile',
        help_text=_("Associated user account")
    )
    
    # Account expiration fields
    expiration_date = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("Date when this account expires")
    )
    is_active_override = models.BooleanField(
        default=True,
        help_text=_("Manual override for account activation status")
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_profiles',
        help_text=_("Admin who created this user account")
    )
    
    class Meta:
        verbose_name = _("User Profile")
        verbose_name_plural = _("User Profiles")
        indexes = [
            models.Index(fields=['expiration_date']),  # For efficient expiration queries
        ]
    
    def __str__(self):
        return f"Profile for {self.user.username}"
    
    @property
    def is_expired(self):
        """Check if user account has expired."""
        if self.expiration_date is None:
            return False
        return timezone.now() > self.expiration_date
    
    @property 
    def is_active(self):
        """Combined activity status based on expiration and manual override."""
        if not self.is_active_override:
            return False
        return not self.is_expired
    
    def set_expiration(self, days):
        """Set expiration date to given number of days from now."""
        if days is None or days <= 0:
            self.expiration_date = None
        else:
            self.expiration_date = timezone.now() + timedelta(days=days)
        self.save()
        
    def extend_expiration(self, days):
        """Extend the current expiration date by the given number of days."""
        if days <= 0:
            return
            
        if self.expiration_date is None:
            self.set_expiration(days)
        else:
            self.expiration_date = self.expiration_date + timedelta(days=days)
            self.save()


class QuestionReport(models.Model):
    """
    Model for users to report issues with questions.
    Allows users to report wrong answers or explanations and suggest corrections.
    """
    
    REPORT_STATUS_CHOICES = [
        ('pending', 'Pending Review'),
        ('reviewed', 'Reviewed'),
        ('resolved', 'Resolved'),
        ('rejected', 'Rejected'),
    ]
    
    question = models.ForeignKey(
        MCQ, 
        on_delete=models.CASCADE, 
        related_name='reports',
        help_text=_("The MCQ being reported")
    )
    user = models.ForeignKey(
        User, 
        on_delete=models.CASCADE,
        help_text=_("User who submitted the report")
    )
    
    # User's report
    suggested_correct_answer = models.CharField(
        max_length=1, 
        choices=[('A', 'A'), ('B', 'B'), ('C', 'C'), ('D', 'D')],
        help_text=_("What the user thinks is the correct answer")
    )
    reason = models.TextField(
        help_text=_("User's explanation of why they think the current answer is wrong")
    )
    
    # Admin tracking
    status = models.CharField(
        max_length=20, 
        choices=REPORT_STATUS_CHOICES, 
        default='pending',
        help_text=_("Current status of this report")
    )
    admin_notes = models.TextField(
        blank=True,
        help_text=_("Internal notes about this report")
    )
    reviewed_by = models.ForeignKey(
        User, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='reviewed_reports',
        help_text=_("Admin who reviewed this report")
    )
    
    # Timestamps
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text=_("When this report was submitted")
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text=_("When this report was last updated")
    )
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = _("Question Report")
        verbose_name_plural = _("Question Reports")
        indexes = [
            models.Index(fields=['status', 'created_at']),
            models.Index(fields=['question', 'user']),
        ]
    
    def __str__(self):
        return f"Report for {self.question.question_number or 'ID:'+str(self.question.id)} by {self.user.username}"


# Signal to create a UserProfile whenever a User is created
@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    """Create a UserProfile when a User is created."""
    if created:
        UserProfile.objects.create(
            user=instance,
            expiration_date=timezone.now() + timedelta(days=30),  # Default 30-day expiration
            created_by=None  # Will be set manually when created from admin
        )


# Case-Based Learning Models
from django.contrib.auth.models import User

class CaseLearningSession(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    session_id = models.CharField(max_length=50, unique=True)
    specialty = models.CharField(max_length=100)
    case_hash = models.CharField(max_length=20)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    final_state = models.CharField(max_length=50, default='INITIAL')
    patient_outcome = models.CharField(max_length=20, choices=[
        ('stable', 'Stable'),
        ('improving', 'Improving'),
        ('deteriorating', 'Deteriorating')
    ], default='stable')
    score = models.IntegerField(default=0)
    
    class Meta:
        ordering = ['-started_at']
        indexes = [
            models.Index(fields=['user', 'specialty']),
            models.Index(fields=['case_hash']),
        ]

class CaseLearningHistory(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    specialty = models.CharField(max_length=100)
    case_hash = models.CharField(max_length=20)
    condition = models.CharField(max_length=200)
    seen_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = ['user', 'case_hash']
        ordering = ['-seen_at']
        indexes = [
            models.Index(fields=['user', 'specialty', '-seen_at']),
        ]


class PersistentCaseLearningSession(models.Model):
    """
    Persistent storage for case-based learning sessions with automatic cleanup
    """
    session_id = models.CharField(max_length=100, unique=True, db_index=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='persistent_case_sessions')
    
    # Session state
    state = models.IntegerField(default=0)
    specialty = models.CharField(max_length=100)
    difficulty = models.CharField(max_length=20)
    
    # Case data
    case_data = models.JSONField(default=dict)
    chief_complaint = models.TextField(blank=True)
    
    # Progress tracking
    history_gathered = models.JSONField(default=list)
    examination_findings = models.JSONField(default=list)
    localization = models.JSONField(default=list)
    investigations = models.JSONField(default=list)
    differentials = models.JSONField(default=list)
    management = models.JSONField(default=list)
    
    # Critical elements tracking
    critical_history_missed = models.JSONField(default=list)
    critical_exam_missed = models.JSONField(default=list)
    missed_critical_steps = models.JSONField(default=list)
    
    # Conversation history (limited to last 50 messages)
    messages = models.JSONField(default=list)
    
    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    last_activity = models.DateTimeField(auto_now=True)
    
    # Cleanup tracking
    completed = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)
    auto_delete_after = models.DateTimeField(null=True, blank=True)
    archived = models.BooleanField(default=False)
    
    # Additional state
    screening_exam_done = models.BooleanField(default=False)
    detailed_exam_areas = models.JSONField(default=list)
    patient_condition = models.CharField(max_length=50, default='stable')
    
    # Message management
    message_count = models.IntegerField(default=0)
    last_cleanup = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        ordering = ['-last_activity']
        verbose_name = "Persistent Case Session"
        verbose_name_plural = "Persistent Case Sessions"
        indexes = [
            models.Index(fields=['user', '-last_activity']),
            models.Index(fields=['session_id']),
            models.Index(fields=['completed', 'auto_delete_after']),
        ]
    
    def save(self, *args, **kwargs):
        # Set auto-delete date when completed
        if self.completed and not self.completed_at:
            self.completed_at = timezone.now()
            self.auto_delete_after = timezone.now() + timedelta(days=7)
        
        # Set auto-delete for abandoned sessions
        elif not self.completed:
            inactive_threshold = timezone.now() - timedelta(hours=72)
            last_activity_value = self.last_activity or timezone.now()
            if last_activity_value < inactive_threshold:
                self.auto_delete_after = timezone.now() + timedelta(hours=1)
        
        super().save(*args, **kwargs)
    
    def __str__(self):
        return f"{self.user.username} - {self.specialty} - {self.created_at}"
