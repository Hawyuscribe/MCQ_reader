from django.urls import path
from . import views
# Import enhanced version from case_bot_enhanced
from .case_bot_enhanced import (
    case_based_learning_enhanced as case_based_learning, 
    neurology_bot_enhanced as neurology_bot, 
    transcribe_audio_enhanced as transcribe_audio
)
from .high_yield_views import high_yield_home, high_yield_specialty, high_yield_topic
from django.contrib.admin.views.decorators import staff_member_required
from .views import openai_selftest

urlpatterns = [
    path('', views.index, name='index'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    # Removed registration path to disable self-registration
    # path('register/', views.register_view, name='register'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('search/', views.search, name='search'),
    path('subspecialty/<path:subspecialty>/', views.subspecialty_view, name='subspecialty'),
    path('mcq/<int:mcq_id>/', views.view_mcq, name='view_mcq'),
    path('mcq/<int:mcq_id>/test_image/', views.test_image_display, name='test_image_display'),
    path('mcq/<int:mcq_id>/check_answer/', views.check_answer, name='check_answer'),
    path('mcq/<int:mcq_id>/toggle_bookmark/', views.toggle_bookmark, name='toggle_bookmark'),
    path('mcq/<int:mcq_id>/save_note/', views.save_note, name='save_note'),
    path('mcq/<int:mcq_id>/create_flashcard/', views.create_flashcard, name='create_flashcard'),
    path('mcq/<int:mcq_id>/reclassify/', views.reclassify_mcq, name='reclassify_mcq'),
    path('mcq/<int:mcq_id>/create_explanation/', views.create_explanation, name='create_explanation'),
    path('mcq/<int:mcq_id>/check_explanation/', views.check_explanation, name='check_explanation'),
    path('mcq/<int:mcq_id>/improve/', views.improve_mcq_view, name='improve_mcq'),
    path('mcq/<int:mcq_id>/new_options/', views.new_options_view, name='new_options'),
    # Removed verify_answer endpoint
    path('mcq/<int:mcq_id>/ask_gpt/', views.ask_gpt, name='ask_gpt'),
    path('mcq/<int:mcq_id>/ask_gpt_async/', views.ask_gpt_async, name='ask_gpt_async'),
    path('ai/job/<str:job_id>/status/', views.ai_job_status, name='ai_job_status'),
    path('review_flashcards/', views.review_flashcards, name='review_flashcards'),
    path('review_bookmarked/', views.review_bookmarked, name='review_bookmarked'),
    path('diagnostics/', views.diagnostics_view, name='diagnostics'),
    path('import/', views.import_mcqs_form, name='import_mcqs_form'),
    # ReasoningPal URLs - New Cognitive Analysis System
    path('mcq/<int:mcq_id>/reasoning_pal/', views.reasoning_pal, name='reasoning_pal'),
    path('mcq/<int:mcq_id>/check_reasoning/', views.check_reasoning, name='check_reasoning'),
    path('reasoning_session/<int:session_id>/feedback/', views.reasoning_feedback, name='reasoning_feedback'),
    
    # New Cognitive ReasoningPal URLs
    path('cognitive_session/<int:session_id>/next_step/', views.reasoning_next_step, name='reasoning_next_step'),
    path('cognitive_session/<int:session_id>/feedback/', views.reasoning_feedback, name='cognitive_reasoning_feedback'),
    path('cognitive_session/<int:session_id>/status/', views.check_reasoning_task_status, name='check_reasoning_task_status'),
    path('cognitive_session/test_worker_connectivity/', views.test_worker_connectivity, name='test_worker_connectivity'),
    path('cognitive_session/check_failed_sessions/', views.check_failed_sessions, name='check_failed_sessions'),
    
    # Test My Understanding URLs
    path('mcq/<int:mcq_id>/generate_test_question/', views.generate_test_question, name='generate_test_question'),
    path('mcq/<int:mcq_id>/generate_test_question_async/', views.generate_test_question_async, name='generate_test_question_async'),
    path('mcq/<int:mcq_id>/analyze_test_reasoning/', views.analyze_test_reasoning, name='analyze_test_reasoning'),
    path('mcq/<int:mcq_id>/analyze_test_reasoning_async/', views.analyze_test_reasoning_async, name='analyze_test_reasoning_async'),
    
    # Hide/Unhide MCQ URLs
    path('mcq/<int:mcq_id>/hide/', views.hide_mcq, name='hide_mcq'),
    path('mcq/<int:mcq_id>/unhide/', views.unhide_mcq, name='unhide_mcq'),
    path('hidden_mcqs/', views.view_hidden_mcqs, name='view_hidden_mcqs'),
    
    # Mock Examination URLs
    path('submit_exam/', views.submit_exam, name='submit_exam'),

    # Test My Weakness URLs
    path('test_weakness/', views.test_weakness, name='test_weakness'),
    path('mcq/<int:mcq_id>/check_weakness_answer/', views.check_weakness_answer, name='check_weakness_answer'),
    
    # Report Question URLs
    path('mcq/<int:mcq_id>/report/', views.show_report_form, name='show_report_form'),
    path('mcq/<int:mcq_id>/submit_report/', views.report_question, name='report_question'),
    
    # Case-Based Learning URLs - using enhanced version
    path('case-based-learning/', case_based_learning, name='case_based_learning'),
    path('api/neurology-bot/', neurology_bot, name='neurology_bot'),
    path('api/neurology-bot-enhanced/', neurology_bot, name='neurology_bot_enhanced'),
    path('api/transcribe-audio/', transcribe_audio, name='transcribe_audio'),
    path('api/transcribe-audio-enhanced/', transcribe_audio, name='transcribe_audio_enhanced'),
    
    # Case Session Management URLs
    path('api/case-sessions/', views.list_case_sessions, name='list_case_sessions'),
    path('api/case-sessions/resume/', views.resume_case_session, name='resume_case_session'),
    path('api/case-sessions/delete/', views.delete_case_session, name='delete_case_session'),
    
    # MCQ to Case Learning Conversion
    path('mcq/<int:mcq_id>/convert-to-case/', views.mcq_to_case_learning, name='mcq_to_case_learning'),
    path('mcq-conversion/status/<int:session_id>/', views.check_mcq_conversion_status, name='check_mcq_conversion_status'),
    
    # High-Yield Reviews URLs
    path('high-yield/', high_yield_home, name='high_yield_home'),
    path('high-yield/<slug:specialty_slug>/', high_yield_specialty, name='high_yield_specialty'),
    path('high-yield/<slug:specialty_slug>/<slug:topic_slug>/', high_yield_topic, name='high_yield_topic'),
    
    # Admin Export URLs
    path('admin-export/', views.admin_export_page, name='admin_export_page'),
    path('export/vascular-mcqs/', views.export_vascular_mcqs, name='export_vascular_mcqs'),
    path('export/subspecialty/<path:subspecialty>/', views.export_subspecialty_mcqs, name='export_subspecialty_mcqs'),
    
    # Admin Import URLs
    path('admin/clear-mcqs/', views.clear_mcqs, name='clear_mcqs'),
    path('admin/import-mcqs-batch/', views.import_mcqs_batch, name='import_mcqs_batch'),
    
    # Inline MCQ Editing URLs (Admin only)
    path('mcq/<int:mcq_id>/update/question/', views.update_mcq_question, name='update_mcq_question'),
    path('mcq/<int:mcq_id>/update/options/', views.update_mcq_options, name='update_mcq_options'),
    path('mcq/<int:mcq_id>/update/explanation/', views.update_mcq_explanation, name='update_mcq_explanation'),
    path('mcq/<int:mcq_id>/update/image/', views.update_mcq_image, name='update_mcq_image'),
    path('mcq/<int:mcq_id>/update/metadata/', views.update_mcq_metadata, name='update_mcq_metadata'),
    
    # AI-powered editing URLs (Admin only)
    path('mcq/<int:mcq_id>/ai/edit/question/', views.ai_edit_mcq_question, name='ai_edit_mcq_question'),
    path('mcq/<int:mcq_id>/ai/edit/options/', views.ai_edit_mcq_options, name='ai_edit_mcq_options'),
    path('mcq/<int:mcq_id>/ai/edit/explanation/', views.ai_edit_mcq_explanation, name='ai_edit_mcq_explanation'),
    path('mcq/<int:mcq_id>/ai/regenerate-all-explanations/', views.regenerate_all_explanations, name='regenerate_all_explanations'),
    path('mcq/ai/jobs/<uuid:job_id>/', views.ai_job_status, name='ai_job_status'),
    
    # Debug URL (TEMPORARY)
    path('debug/mcq-ids/', views.debug_mcq_ids, name='debug_mcq_ids'),
    
    # Admin Debug Console URLs (Staff only)
    path('admin/debug/', views.admin_debug_console, name='admin_debug_console'),
    path('admin/debug/events/', views.admin_debug_events_api, name='admin_debug_events'),
    path('admin/debug/log-events/', views.admin_log_client_event, name='admin_log_client_event'),
    path('admin/debug/session-integrity/', views.debug_session_integrity, name='debug_session_integrity'),
    path('admin/debug/clear-cache/', views.debug_clear_session_cache, name='debug_clear_session_cache'),
    path('admin/debug/trace-mcq/<int:mcq_id>/', views.debug_trace_mcq_conversion, name='debug_trace_mcq_conversion'),
    path('admin/debug/tracking-report/<str:tracking_id>/', views.get_tracking_report, name='get_tracking_report'),
    path('admin/debug/live-tracking/<int:mcq_id>/', views.live_tracking_data, name='live_tracking_data'),
    path('admin/debug/export-tracking/', views.export_tracking_data, name='export_tracking_data'),
    path('admin/debug/recent-tracking/', views.recent_tracking_data, name='recent_tracking_data'),
    path('admin/debug/search-tracking/', views.search_tracking_data, name='search_tracking_data'),
    
    # Heroku Testing URLs (Staff only)
    path('admin/debug/test-heroku/', views.test_heroku_mcq_conversion, name='test_heroku_mcq_conversion'),
    path('admin/debug/monitor-test/<int:session_id>/', views.monitor_heroku_test, name='monitor_heroku_test'),
    path('admin/debug/heroku-tester/', views.heroku_test_interface, name='heroku_test_interface'),
    # OpenAI diagnostics (Staff only) - placed outside /admin to avoid admin URL shadowing
    path('debug/openai-selftest/', staff_member_required(openai_selftest), name='openai_selftest'),
    # Clinical Reasoning debug APIs (staff or token)
    path('debug/clinical-reasoning/run/', views.debug_run_clinical_reasoning, name='debug_run_clinical_reasoning'),
    path('debug/clinical-reasoning/session/<int:session_id>/', views.debug_reasoning_session, name='debug_reasoning_session'),
]
