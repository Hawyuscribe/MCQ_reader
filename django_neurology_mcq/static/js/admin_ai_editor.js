'use strict';

(function (window, document) {
  const DEFAULT_REQUEST_TIMEOUT = 60000;

  const state = {
    mcqId: null,
    csrfToken: null,
    endpoints: {},
    question: {
      textarea: null,
      display: null,
      originalText: '',
      lastSuccessfulText: '',
      minChars: 220,
      minWords: 45
    },
    options: {
      editors: {},
      correctSelect: null
    },
    explanation: {
      textarea: null,
      charCountEl: null,
      lastSuccessfulText: '',
      recorder: null,
      recordStatusEl: null
    },
    modals: {
      instruction: null,
      options: null
    },
    instructionRecorder: null
  };

  /* -------------------------------------------------------------------------- */
  /* Helpers                                                                     */
  /* -------------------------------------------------------------------------- */

  function getCookie(name) {
    const cookies = document.cookie ? document.cookie.split(';') : [];
    for (let i = 0; i < cookies.length; i += 1) {
      const cookie = cookies[i].trim();
      if (cookie.substring(0, name.length + 1) === `${name}=`) {
        return decodeURIComponent(cookie.substring(name.length + 1));
      }
    }
    return null;
  }

  function adminDebugLog(message, type) {
    if (typeof window.adminDebugLog === 'function') {
      window.adminDebugLog(message, type);
      return;
    }
    if (type === 'error' && console.error) {
      console.error('[AdminDebug]', message);
    } else if (type === 'warn' && console.warn) {
      console.warn('[AdminDebug]', message);
    } else if (console.log) {
      console.log('[AdminDebug]', message);
    }
  }

  function truncateForDebug(text, maxLength) {
    if (!text) {
      return '';
    }
    const limit = maxLength || 160;
    const stringified = String(text);
    if (stringified.length <= limit) {
      return stringified;
    }
    return `${stringified.slice(0, limit)}…`;
  }

  function showAlert(message, variant) {
    const alert = document.createElement('div');
    alert.className = `alert alert-${variant} alert-dismissible fade show position-fixed top-0 start-50 translate-middle-x mt-3`;
    alert.style.zIndex = '9999';
    alert.innerHTML = `
      <span>${message}</span>
      <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
    `;
    document.body.appendChild(alert);
    window.setTimeout(() => {
      if (alert && alert.remove) {
        alert.remove();
      }
    }, variant === 'success' ? 3000 : 5000);
  }

  function showSuccessMessage(message) {
    showAlert(`<i class="bi bi-check-circle"></i> ${message}`, 'success');
  }

  function showWarningMessage(message) {
    showAlert(`<i class="bi bi-exclamation-triangle"></i> ${message}`, 'warning');
  }

  function showLoadingMessage(message) {
    let alertDiv = document.getElementById('loadingMessage');
    if (!alertDiv) {
      alertDiv = document.createElement('div');
      alertDiv.id = 'loadingMessage';
      alertDiv.className = 'alert alert-info alert-dismissible fade show position-fixed top-0 start-50 translate-middle-x mt-3';
      alertDiv.style.zIndex = '9999';
      document.body.appendChild(alertDiv);
    }

    alertDiv.innerHTML = `
      <div class="d-flex align-items-center">
        <div class="spinner-border spinner-border-sm me-2" role="status">
          <span class="visually-hidden">Loading...</span>
        </div>
        ${message}
      </div>
    `;

    window.setTimeout(() => {
      const existing = document.getElementById('loadingMessage');
      if (existing && existing.remove) {
        existing.remove();
      }
    }, 10000);
  }

  function toggleButtonLoading(button, isLoading, defaultHtml, loadingLabel) {
    if (!button) {
      return;
    }
    if (isLoading) {
      button.dataset.defaultHtml = defaultHtml || button.innerHTML;
      button.disabled = true;
      button.innerHTML = `
        <span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>
        ${loadingLabel || 'Working...'}
      `;
    } else {
      const original = button.dataset.defaultHtml || defaultHtml;
      if (original) {
        button.innerHTML = original;
      }
      button.disabled = false;
    }
  }

  function renderQuestionDisplay(rawText) {
    const displayEl = state.question.display || document.getElementById('questionDisplay');
    if (!displayEl) return;

    if (!rawText) {
      displayEl.innerHTML = '<span class="text-muted">No question text available.</span>';
      return;
    }

    const paragraphs = String(rawText)
      .split(/\n{2,}/)
      .map((part) => part.trim())
      .filter(Boolean);

    const html = (paragraphs.length ? paragraphs : [rawText.trim()])
      .map((paragraph) => `<p>${paragraph.replace(/\n/g, '<br>')}</p>`)
      .join('');

    displayEl.innerHTML = html;
  }

  function updateExplanationMetrics() {
    const textarea = state.explanation.textarea;
    if (!textarea) return;
    const value = textarea.value || '';
    const charCount = value.length;
    const wordCount = value.trim() ? value.trim().split(/\s+/).length : 0;
    const counter = state.explanation.charCountEl;
    if (counter) {
      counter.textContent = `${charCount} characters • ${wordCount} words`;
    }
  }

  function getUnifiedExplanationText() {
    return state.explanation.textarea ? state.explanation.textarea.value.trim() : '';
  }

  function setUnifiedExplanationText(text) {
    if (!state.explanation.textarea) return;
    state.explanation.textarea.value = text || '';
    state.explanation.textarea.classList.remove('is-invalid');
    updateExplanationMetrics();
  }

  async function postJson(url, payload, label) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), DEFAULT_REQUEST_TIMEOUT);

    try {
      const response = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRFToken': state.csrfToken
        },
        body: JSON.stringify(payload),
        signal: controller.signal
      });

      const text = await response.text();
      let data = {};
      if (text) {
        try {
          data = JSON.parse(text);
        } catch (err) {
          adminDebugLog(`❌ [${label}] Invalid JSON response: ${err}`, 'error');
          throw new Error('Unable to parse server response.');
        }
      }

      if (!response.ok) {
        const message = data.error || `HTTP ${response.status}`;
        adminDebugLog(`❌ [${label}] ${message}`, 'error');
        throw new Error(message);
      }

      adminDebugLog(`📥 [${label}] Response: ${truncateForDebug(text, 200) || '[empty]'}`);
      return data;
    } catch (error) {
      if (error.name === 'AbortError') {
        adminDebugLog(`❌ [${label}] Request timed out`, 'error');
        throw new Error('Request timed out. Please try again.');
      }
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
  }

  function ensureEndpoint(name) {
    if (!state.endpoints[name]) {
      throw new Error(`Endpoint '${name}' is not configured.`);
    }
    return state.endpoints[name];
  }

  const JOB_STATUS_PLACEHOLDER = '00000000-0000-0000-0000-000000000000';

  function buildJobStatusUrl(jobId) {
    const template = ensureEndpoint('jobStatus');
    if (!template) {
      throw new Error('Job status endpoint is not configured.');
    }
    return template.replace(JOB_STATUS_PLACEHOLDER, jobId);
  }

  async function fetchJobStatus(jobId, label) {
    const response = await fetch(buildJobStatusUrl(jobId), {
      headers: { 'X-Requested-With': 'XMLHttpRequest' }
    });
    if (!response.ok) {
      throw new Error(`Job status request failed (HTTP ${response.status})`);
    }
    const data = await response.json();
    adminDebugLog(`⏳ [${label}] Job ${jobId} status: ${data.status}`);
    return data;
  }

  async function waitForJobCompletion(jobId, label) {
    let attempts = 0;
    while (true) {
      const info = await fetchJobStatus(jobId, label);
      if (info.status === 'succeeded') {
        return info.result || {};
      }
      if (info.status === 'failed') {
        throw new Error(info.error || 'AI job failed.');
      }
      const delayMs = Math.min(2000 + attempts * 500, 6000);
      await new Promise((resolve) => setTimeout(resolve, delayMs));
      attempts += 1;
    }
  }

  /* -------------------------------------------------------------------------- */
  /* Instruction Modal + Recorder                                               */
  /* -------------------------------------------------------------------------- */

  function createInstructionRecorder() {
    const recorderState = {
      mediaRecorder: null,
      audioChunks: [],
      mimeType: '',
      stream: null,
      isRecording: false,
      statusEl: null,
      recordBtn: null
    };

    async function start(statusEl, recordBtn) {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        alert('Audio recording is not supported in this browser.');
        return;
      }

      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        recorderState.stream = stream;
        const mimeTypes = [
          'audio/webm;codecs=opus',
          'audio/webm',
          'audio/ogg',
          'audio/mp4',
          'audio/m4a',
          'audio/wav',
          'audio/mp3'
        ];
        let options = {};
        let selectedMime = '';
        mimeTypes.some((type) => {
          if (MediaRecorder.isTypeSupported(type)) {
            options = { mimeType: type };
            selectedMime = type;
            return true;
          }
          return false;
        });

        recorderState.mediaRecorder = selectedMime
          ? new MediaRecorder(stream, options)
          : new MediaRecorder(stream);
        recorderState.mimeType =
          selectedMime || recorderState.mediaRecorder.mimeType || 'audio/webm';
        recorderState.audioChunks = [];
        recorderState.isRecording = true;
        recorderState.statusEl = statusEl || null;
        recorderState.recordBtn = recordBtn || null;

        recorderState.mediaRecorder.ondataavailable = (event) => {
          if (event.data && event.data.size > 0) {
            recorderState.audioChunks.push(event.data);
          }
        };

        recorderState.mediaRecorder.start(1000);
        if (recorderState.recordBtn) {
          recorderState.recordBtn.classList.add('btn-danger');
          recorderState.recordBtn.innerHTML = '<i class="bi bi-stop-circle"></i> Stop';
        }
        if (recorderState.statusEl) {
          recorderState.statusEl.textContent = 'Recording… click stop when finished.';
          recorderState.statusEl.classList.add('active');
        }
      } catch (error) {
        console.error('Instruction recording error:', error);
        alert('Unable to start recording. Please ensure microphone access is allowed.');
      }
    }

    async function stop(skipTranscription) {
      if (!recorderState.isRecording) return;
      recorderState.isRecording = false;

      if (recorderState.mediaRecorder && recorderState.mediaRecorder.state !== 'inactive') {
        await new Promise((resolve) => {
          recorderState.mediaRecorder.addEventListener('stop', resolve, { once: true });
          recorderState.mediaRecorder.stop();
        });
      }

      if (recorderState.stream) {
        recorderState.stream.getTracks().forEach((track) => track.stop());
      }
      if (!skipTranscription && recorderState.audioChunks.length) {
        return finalize();
      }
      reset();
      return null;
    }

    async function finalize() {
      if (!recorderState.audioChunks.length) {
        reset();
        return null;
      }
      const blob = new Blob(recorderState.audioChunks, { type: recorderState.mimeType });
      try {
        const transcription = await transcribeAudio(blob, recorderState.mimeType, 'ai-instructions');
        if (recorderState.statusEl) {
          recorderState.statusEl.textContent = 'Transcription complete.';
        }
        return transcription;
      } catch (error) {
        if (recorderState.statusEl) {
          recorderState.statusEl.textContent = 'Unable to transcribe audio.';
        }
        alert('Unable to transcribe the recording. Please try again.');
        return null;
      } finally {
        reset();
      }
    }

    function reset() {
      recorderState.audioChunks = [];
      recorderState.mimeType = '';
      recorderState.mediaRecorder = null;
      recorderState.stream = null;
      recorderState.isRecording = false;
      if (recorderState.recordBtn) {
        recorderState.recordBtn.classList.remove('btn-danger');
        recorderState.recordBtn.innerHTML = '<i class="bi bi-mic"></i> Record';
      }
      if (recorderState.statusEl) {
        recorderState.statusEl.textContent = '';
        recorderState.statusEl.classList.remove('active');
      }
      recorderState.recordBtn = null;
      recorderState.statusEl = null;
    }

    return {
      start,
      stop,
      reset,
      get isRecording() {
        return recorderState.isRecording;
      }
    };
  }

  function setupInstructionModal() {
    const modalEl = document.getElementById('aiInstructionModal');
    if (!modalEl) {
      adminDebugLog('AI instruction modal element not found.', 'error');
      return;
    }
    const titleEl = modalEl.querySelector('#aiInstructionModalLabel');
    const descEl = modalEl.querySelector('#aiInstructionModalDescription');
    const textarea = modalEl.querySelector('#aiInstructionTextarea');
    const confirmBtn = modalEl.querySelector('#aiInstructionConfirmBtn');
    const recordBtn = modalEl.querySelector('#aiInstructionRecordBtn');
    const statusEl = modalEl.querySelector('#aiInstructionRecordStatus');
    const modalInstance = new bootstrap.Modal(modalEl);

    state.instructionRecorder = createInstructionRecorder();

    function resetModal() {
      if (textarea) {
        textarea.value = '';
        textarea.classList.remove('is-valid', 'is-invalid');
      }
      if (statusEl) {
        statusEl.textContent = '';
        statusEl.classList.remove('active');
      }
      if (state.instructionRecorder) {
        state.instructionRecorder.reset();
      }
    }

    async function handleRecordingToggle() {
      if (!state.instructionRecorder) return;
      if (state.instructionRecorder.isRecording) {
        const transcription = await state.instructionRecorder.stop(false);
        if (transcription && textarea) {
          textarea.value = `${textarea.value ? `${textarea.value.trim()}\n\n` : ''}${transcription}`.trim();
          textarea.dispatchEvent(new Event('input'));
          showSuccessMessage('Recording transcribed into the instructions field.');
        }
        return;
      }
      await state.instructionRecorder.start(statusEl, recordBtn);
    }

    state.modals.instruction = {
      async prompt(config) {
        if (!modalInstance) {
          alert('Unable to open AI instruction modal.');
          return { confirmed: false, instructions: '' };
        }

        if (titleEl) {
          titleEl.textContent = config.title || 'AI Custom Instructions';
        }
        if (descEl) {
          descEl.textContent =
            config.description || 'Provide optional guidance for the AI.';
        }
        if (textarea) {
          textarea.value = config.defaultValue || '';
          textarea.placeholder =
            config.placeholder || 'Optional: describe how the AI should tailor its edits.';
        }

        resetModal();

        return await new Promise((resolve) => {
          const confirmHandler = () => {
            state.modals.instruction._confirmed = true;
            modalInstance.hide();
          };

          const handleHidden = () => {
            modalEl.removeEventListener('hidden.bs.modal', handleHidden);
            modalEl.removeEventListener('shown.bs.modal', handleShown);
            if (confirmBtn) {
              confirmBtn.removeEventListener('click', confirmHandler);
            }
            if (recordBtn) {
              recordBtn.removeEventListener('click', handleRecordingToggle);
            }
            if (state.instructionRecorder && state.instructionRecorder.isRecording) {
              state.instructionRecorder.stop(true);
            }
            const instructions = textarea ? textarea.value.trim() : '';
            resolve({
              confirmed: state.modals.instruction._confirmed === true,
              instructions
            });
            state.modals.instruction._confirmed = false;
            resetModal();
          };

          const handleShown = () => {
            modalEl.removeEventListener('shown.bs.modal', handleShown);
            if (textarea) {
              textarea.focus();
            }
          };

          modalEl.addEventListener('hidden.bs.modal', handleHidden);
          modalEl.addEventListener('shown.bs.modal', handleShown);

          if (confirmBtn) {
            confirmBtn.addEventListener('click', confirmHandler);
          }

          if (recordBtn) {
            recordBtn.addEventListener('click', handleRecordingToggle);
          }

          modalInstance.show();
        });
      }
    };
  }

  /* -------------------------------------------------------------------------- */
  /* Explanation Recorder                                                       */
  /* -------------------------------------------------------------------------- */

  function setupExplanationRecorder() {
    state.explanation.recorder = {
      isRecording: false,
      mediaRecorder: null,
      audioChunks: [],
      mimeType: '',
      activeTextareaId: null,
      activeButton: null,
      stream: null
    };

    async function start(button, textareaId) {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        alert('Voice recording is not supported in this browser.');
        return;
      }

      const recorder = state.explanation.recorder;
      if (recorder.isRecording) {
        await stop();
        return;
      }

      const statusEl = state.explanation.recordStatusEl;

      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        recorder.stream = stream;

        const mimeTypes = [
          'audio/webm;codecs=opus',
          'audio/webm',
          'audio/ogg',
          'audio/mp4',
          'audio/m4a',
          'audio/wav',
          'audio/mp3'
        ];
        let options = {};
        let selectedMime = '';
        mimeTypes.some((type) => {
          if (MediaRecorder.isTypeSupported(type)) {
            options = { mimeType: type };
            selectedMime = type;
            return true;
          }
          return false;
        });

        const mediaRecorder = selectedMime
          ? new MediaRecorder(stream, options)
          : new MediaRecorder(stream);

        recorder.mediaRecorder = mediaRecorder;
        recorder.mimeType = selectedMime || mediaRecorder.mimeType || 'audio/webm';
        recorder.audioChunks = [];
        recorder.isRecording = true;
        recorder.activeTextareaId = textareaId;
        recorder.activeButton = button;

        mediaRecorder.ondataavailable = (event) => {
          if (event.data && event.data.size > 0) {
            recorder.audioChunks.push(event.data);
          }
        };

        mediaRecorder.addEventListener(
          'stop',
          async () => {
            if (!recorder.audioChunks.length) {
              reset();
              return;
            }

            const blob = new Blob(recorder.audioChunks, { type: recorder.mimeType });
            try {
              const transcription = await transcribeAudio(
                blob,
                recorder.mimeType,
                'mcq-explanation'
              );
              const textarea = document.getElementById(recorder.activeTextareaId);
              if (textarea && transcription) {
                textarea.value = `${textarea.value ? `${textarea.value.trim()}\n\n` : ''}${transcription}`.trim();
                textarea.dispatchEvent(new Event('input'));
                showSuccessMessage('Transcription added to the explanation.');
              }
              if (statusEl) {
                statusEl.textContent = 'Transcription added.';
              }
            } catch (error) {
              if (statusEl) {
                statusEl.textContent = 'Unable to transcribe audio.';
              }
              alert('Unable to transcribe the recording. Please try again.');
            } finally {
              reset();
            }
          },
          { once: true }
        );

        try {
          mediaRecorder.start(1000);
        } catch (startError) {
          mediaRecorder.start();
        }

        if (button) {
          button.classList.add('explanation-recording-active');
          button.innerHTML = '<i class="bi bi-stop-circle"></i> Stop';
        }

        if (statusEl) {
          statusEl.innerHTML =
            '<span class="spinner-border spinner-border-sm me-1" role="status"></span>Recording… click stop when done.';
        }
      } catch (error) {
        console.error('Explanation recording error', error);
        alert('Unable to start recording. Please ensure microphone access is allowed.');
        reset();
      }
    }

    async function stop() {
      const recorder = state.explanation.recorder;
      if (!recorder.isRecording) return;

      recorder.isRecording = false;

      if (recorder.mediaRecorder && recorder.mediaRecorder.state !== 'inactive') {
        recorder.mediaRecorder.stop();
      }

      if (recorder.stream) {
        recorder.stream.getTracks().forEach((track) => track.stop());
      }
    }

    function reset() {
      const recorder = state.explanation.recorder;
      const statusEl = state.explanation.recordStatusEl;

      if (recorder.activeButton) {
        recorder.activeButton.classList.remove('explanation-recording-active');
        recorder.activeButton.innerHTML = '<i class="bi bi-mic"></i> Record';
      }
      if (statusEl) {
        statusEl.textContent = '';
      }

      recorder.mediaRecorder = null;
      recorder.audioChunks = [];
      recorder.mimeType = '';
      recorder.activeTextareaId = null;
      recorder.activeButton = null;
      if (recorder.stream) {
        recorder.stream.getTracks().forEach((track) => track.stop());
      }
      recorder.stream = null;
      recorder.isRecording = false;
    }

    state.explanation.recorder.start = start;
    state.explanation.recorder.stop = stop;
    state.explanation.recorder.reset = reset;
  }

  async function transcribeAudio(blob, mimeType, filenamePrefix) {
    const formData = new FormData();
    const extension = mimeType.includes('mpeg') || mimeType.includes('mp3') ? '.mp3' : '.webm';
    formData.append('audio', blob, `${filenamePrefix || 'recording'}${extension}`);
    formData.append('mimeType', mimeType);

    const response = await fetch(ensureEndpoint('transcribe'), {
      method: 'POST',
      headers: {
        'X-CSRFToken': state.csrfToken
      },
      body: formData
    });

    if (!response.ok) {
      throw new Error(`Transcription failed (HTTP ${response.status})`);
    }

    const data = await response.json();
    if (!data || !data.text) {
      throw new Error('Transcription returned empty response.');
    }
    return data.text;
  }

  /* -------------------------------------------------------------------------- */
  /* CRUD helpers (question/options/explanation/image)                          */
  /* -------------------------------------------------------------------------- */

  async function saveQuestion() {
    const textarea = state.question.textarea;
    if (!textarea) return;

    const questionText = textarea.value.trim();
    if (!questionText) {
      alert('Question text cannot be empty.');
      return;
    }

    try {
      const data = await postJson(
        ensureEndpoint('saveQuestion'),
        { question_text: questionText },
        'Save Question'
      );
      if (data.success) {
        renderQuestionDisplay(data.question_text);
        const editSection = document.getElementById('questionEditSection');
        if (editSection) {
          editSection.style.display = 'none';
        }
        state.question.lastSuccessfulText = data.question_text;
        showSuccessMessage('Question updated successfully.');
      } else {
        throw new Error(data.error || 'Unable to update question.');
      }
    } catch (error) {
      alert(`Error updating question: ${error.message}`);
    }
  }

  async function saveOptions() {
    if (!Object.keys(state.options.editors).length) {
      alert('Option editors not found.');
      return;
    }

    const optionValues = {};
    const missing = [];
    ['A', 'B', 'C', 'D'].forEach((letter) => {
      const editor = state.options.editors[letter];
      const value = editor ? editor.value.trim() : '';
      optionValues[letter.toLowerCase()] = value;
      if (!value) {
        missing.push(letter);
      }
    });

    if (missing.length) {
      alert(`All options must be filled. Missing: ${missing.join(', ')}`);
      return;
    }

    const payload = {
      option_a: optionValues.a,
      option_b: optionValues.b,
      option_c: optionValues.c,
      option_d: optionValues.d,
      correct_answer: state.options.correctSelect ? state.options.correctSelect.value : 'A'
    };

    try {
      const data = await postJson(
        ensureEndpoint('saveOptions'),
        payload,
        'Save Options'
      );
      if (data.success) {
        window.location.reload();
      } else {
        throw new Error(data.error || 'Unable to update options.');
      }
    } catch (error) {
      alert(`Error updating options: ${error.message}`);
    }
  }

  async function saveImage() {
    const input = document.getElementById('imageUrlEdit');
    if (!input) return;

    const imageUrl = input.value.trim();
    try {
      const data = await postJson(
        ensureEndpoint('saveImage'),
        { image_url: imageUrl },
        'Save Image'
      );
      if (data.success) {
        window.location.reload();
      } else {
        throw new Error(data.error || 'Unable to update image.');
      }
    } catch (error) {
      alert(`Error updating image: ${error.message}`);
    }
  }

  function removeImage() {
    if (confirm('Are you sure you want to remove the image?')) {
      const input = document.getElementById('imageUrlEdit');
      if (input) {
        input.value = '';
      }
      saveImage();
    }
  }

  async function saveExplanation() {
    const textarea = state.explanation.textarea;
    if (!textarea) {
      alert('Explanation editor not found.');
      return;
    }

    const explanationText = textarea.value.trim();
    if (!explanationText) {
      const proceed = confirm(
        'The explanation is currently empty. Do you still want to save and clear the existing content?'
      );
      if (!proceed) {
        return;
      }
    } else {
      textarea.classList.remove('is-invalid');
    }

    const saveButton = document.querySelector('#explanationEditSection button.btn-success');
    toggleButtonLoading(saveButton, true, '<i class="bi bi-check-circle"></i> Save explanation');

    try {
      const data = await postJson(
        ensureEndpoint('saveExplanation'),
        { explanation: explanationText },
        'Save Explanation'
      );
      if (data.success) {
        state.explanation.lastSuccessfulText = explanationText;
        const explanationCard = document.getElementById('explanation');
        if (explanationCard) {
          explanationCard.classList.add('explanation-updated');
        }

        const explanationContent = document.querySelector('#explanation .explanation-content');
        if (explanationContent) {
          if (data.html_preview) {
            explanationContent.innerHTML = data.html_preview;
          } else if (data.unified_explanation) {
            explanationContent.innerHTML = data.unified_explanation.replace(/\n/g, '<br>');
          } else if (!explanationText) {
            explanationContent.innerHTML =
              '<p class="text-muted">No explanation provided for this MCQ yet.</p>';
          }
        }

        showSuccessMessage('Explanation saved.');
        cancelEdit('explanation');
      } else {
        throw new Error(data.error || 'Unable to save explanation.');
      }
    } catch (error) {
      alert(`Error updating explanation: ${error.message}`);
    } finally {
      toggleButtonLoading(saveButton, false, '<i class="bi bi-check-circle"></i> Save explanation');
    }
  }

  function cancelEdit(section) {
    const sectionId = `${section}EditSection`;
    const el = document.getElementById(sectionId);
    if (el) {
      el.style.display = 'none';
    }
  }

  /* -------------------------------------------------------------------------- */
  /* AI Handlers                                                                */
  /* -------------------------------------------------------------------------- */

  function validateQuestionResponse(text) {
    if (typeof text !== 'string') return false;
    const trimmed = text.trim();
    if (!trimmed) return false;
    if (trimmed.length < state.question.minChars) return false;
    const words = trimmed.split(/\s+/).filter(Boolean);
    if (words.length < state.question.minWords) return false;
    return true;
  }

  async function handleQuestionAI() {
    const textarea = state.question.textarea;
    if (!textarea) {
      alert('Unable to locate the question editor.');
      return;
    }

    const triggerButton = document.querySelector('.js-ai-question');
    const previousDraft = textarea.value;
    const { confirmed, instructions } = await state.modals.instruction.prompt({
      title: 'AI Question Editor',
      description:
        'Optional: describe how the AI should refine the MCQ stem (tone, focus, target audience). Leave blank to continue.',
      placeholder: 'Example: shorten passive voice, emphasize diagnostic reasoning steps.'
    });

    if (!confirmed) {
      adminDebugLog('⚪️ [Question AI] Cancelled by user.');
      return;
    }

    toggleButtonLoading(triggerButton, true, triggerButton ? triggerButton.innerHTML : '', 'Generating…');
    textarea.disabled = true;

    try {
      const payload = {
        custom_instructions: instructions || ''
      };
      const queueResponse = await postJson(
        ensureEndpoint('question'),
        payload,
        'Question AI'
      );

      const jobId = queueResponse.job_id;
      if (!jobId) {
        throw new Error(queueResponse.error || 'Server did not return a job identifier.');
      }
      adminDebugLog(`🧵 [Question AI] Job queued (${jobId}).`);

      const result = await waitForJobCompletion(jobId, 'Question AI');

      if (result.success && validateQuestionResponse(result.improved_text)) {
        textarea.value = result.improved_text;
        renderQuestionDisplay(result.improved_text);
        state.question.lastSuccessfulText = result.improved_text;
        textarea.classList.remove('is-invalid');
        textarea.classList.add('is-valid');
        showSuccessMessage(result.message || 'AI has improved the question. Review and save when ready.');
      } else {
        const errorMsg =
          (result && result.error) ||
          'AI returned an incomplete response. Your previous draft has been restored.';
        const fallbackText = state.question.lastSuccessfulText || previousDraft;
        textarea.value = fallbackText;
        renderQuestionDisplay(fallbackText);
        textarea.classList.add('is-invalid');
        showWarningMessage(errorMsg);
      }
    } catch (error) {
      const fallbackText = state.question.lastSuccessfulText || previousDraft;
      textarea.value = fallbackText;
      renderQuestionDisplay(fallbackText);
      textarea.classList.add('is-invalid');
      showWarningMessage(error.message || 'AI question editor is unavailable right now.');
    } finally {
      textarea.disabled = false;
      toggleButtonLoading(triggerButton, false, '<i class="bi bi-magic"></i> Edit with AI');
    }
  }

  function getCurrentOptionDrafts() {
    const drafts = {};
    ['A', 'B', 'C', 'D'].forEach((letter) => {
      const editor = state.options.editors[letter];
      drafts[letter] = editor ? editor.value : '';
    });
    return drafts;
  }

  async function openOptionsModeModal() {
    if (!state.modals.options) {
      const modalEl = document.createElement('div');
      modalEl.className = 'modal fade';
      modalEl.id = 'aiOptionsModal';
      modalEl.setAttribute('tabindex', '-1');
      modalEl.innerHTML = `
        <div class="modal-dialog">
          <div class="modal-content">
            <div class="modal-header">
              <h5 class="modal-title">AI Options Editor</h5>
              <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
            </div>
            <div class="modal-body">
              <p>Choose how you want AI to help with the MCQ options:</p>
              <div class="d-grid gap-2">
                <button class="btn btn-primary" data-mode="fill_missing">
                  <i class="bi bi-plus-circle"></i> Fill Missing Options Only
                  <small class="d-block mt-1">AI will only generate USMLE-style distractors for empty options</small>
                </button>
                <button class="btn btn-warning" data-mode="improve_all">
                  <i class="bi bi-arrow-repeat"></i> Improve All Options
                  <small class="d-block mt-1">AI will enhance all incorrect options to be more educationally valuable</small>
                </button>
              </div>
            </div>
          </div>
        </div>
      `;
      document.body.appendChild(modalEl);
      const modalInstance = new bootstrap.Modal(modalEl);
      state.modals.options = { el: modalEl, instance: modalInstance };
    }

    return await new Promise((resolve) => {
      const { el, instance } = state.modals.options;

      function handleClick(event) {
        const button = event.target.closest('button[data-mode]');
        if (!button) return;
        const mode = button.getAttribute('data-mode');
        el.removeEventListener('click', handleClick);
        instance.hide();
        resolve(mode);
      }

      function handleHidden() {
        el.removeEventListener('hidden.bs.modal', handleHidden);
        el.removeEventListener('click', handleClick);
        resolve(null);
      }

      el.addEventListener('click', handleClick);
      el.addEventListener('hidden.bs.modal', handleHidden, { once: true });
      instance.show();
    });
  }

  async function handleOptionsAI() {
    const mode = await openOptionsModeModal();
    if (!mode) {
      adminDebugLog('⚪️ [Options AI] Cancelled by user.');
      return;
    }
    adminDebugLog(`🧠 [Options AI] Mode selected: ${mode}.`);

    const description =
      mode === 'fill_missing'
        ? 'AI will generate USMLE-style distractors for empty options. Optionally highlight themes, distractor patterns, or wording style.'
        : 'AI will enhance every option. Optionally specify tone, difficulty, or pitfalls to emphasize.';

    const { confirmed, instructions } = await state.modals.instruction.prompt({
      title: 'AI Options Editor',
      description,
      placeholder:
        'Example: keep option length similar, include two pharmacology distractors.'
    });

    if (!confirmed) {
      adminDebugLog('⚪️ [Options AI] Cancelled by user.');
      return;
    }

    const button = document.querySelector('.js-ai-options');
    toggleButtonLoading(
      button,
      true,
      button ? button.innerHTML : '',
      mode === 'fill_missing' ? 'Filling options…' : 'Improving options…'
    );

    try {
      const payload = {
        mode,
        custom_instructions: instructions || '',
        question_text: state.question.textarea ? state.question.textarea.value : '',
        current_options: getCurrentOptionDrafts(),
        correct_answer: state.options.correctSelect ? state.options.correctSelect.value : 'A'
      };

      const queueResponse = await postJson(
        ensureEndpoint('options'),
        payload,
        'Options AI'
      );

      const jobId = queueResponse.job_id;
      if (!jobId) {
        throw new Error(queueResponse.error || 'Server did not return a job identifier.');
      }
      adminDebugLog(`🧵 [Options AI] Job queued (${jobId}).`);

      const result = await waitForJobCompletion(jobId, 'Options AI');

      if (result.success && result.options) {
        let updatedCount = 0;
        ['A', 'B', 'C', 'D'].forEach((letter) => {
          const editor = state.options.editors[letter];
          const newValue = (result.options[letter] || '').trim();
          if (editor && newValue) {
            editor.value = newValue;
            updatedCount += 1;
          }
        });

        if (updatedCount > 0) {
          showSuccessMessage(
            result.message ||
              (mode === 'fill_missing'
                ? 'AI filled the requested options with USMLE-style distractors. Review and save when ready.'
                : 'AI improved the distractors. Review and save when ready.')
          );
        } else {
          showWarningMessage(
            result.message || 'AI did not provide any updates. Please refine your instructions.'
          );
        }

        if (result.unified_explanation && result.html_preview) {
          setUnifiedExplanationText(result.unified_explanation);
          const explanationContent = document.querySelector('#explanation .explanation-content');
          if (explanationContent) {
            explanationContent.innerHTML = result.html_preview;
          }
        }
      } else {
        const errorMsg = (result && result.error) || 'AI could not update options.';
        showWarningMessage(errorMsg);
      }
    } catch (error) {
      showWarningMessage(error.message || 'Error using AI for options.');
    } finally {
      toggleButtonLoading(button, false, '<i class="bi bi-magic"></i> Edit Options with AI');
    }
  }

  async function handleRewriteExplanation() {
    const { confirmed, instructions } = await state.modals.instruction.prompt({
      title: 'Rewrite Entire Explanation',
      description:
        'Optional: provide guidance on tone, emphasis, or references before regenerating the explanation.',
      placeholder: 'Example: Highlight electrodiagnostic reasoning and cite 2024 guidelines.'
    });

    if (!confirmed) {
      adminDebugLog('⚪️ [Rewrite AI] Instruction modal dismissed; aborting regeneration.');
      return;
    }

    const rewriteButton = document.getElementById('rewriteExplanationBtn');
    toggleButtonLoading(
      rewriteButton,
      true,
      rewriteButton ? rewriteButton.innerHTML : '',
      'Regenerating…'
    );
    showLoadingMessage('AI is regenerating the explanation...');

    try {
      const payload = {
        custom_instructions: instructions || ''
      };
      const queueResponse = await postJson(
        ensureEndpoint('regenerate'),
        payload,
        'Rewrite AI'
      );

      const jobId = queueResponse.job_id;
      if (!jobId) {
        throw new Error(queueResponse.error || 'Server did not return a job identifier.');
      }
      adminDebugLog(`🧵 [Rewrite AI] Job queued (${jobId}).`);

      const result = await waitForJobCompletion(jobId, 'Rewrite AI');

      if (result.success && (result.unified_explanation || '').trim()) {
        setUnifiedExplanationText(result.unified_explanation.trim());
        state.explanation.lastSuccessfulText = result.unified_explanation.trim();
        if (result.html_preview) {
          const explanationContent = document.querySelector('#explanation .explanation-content');
          if (explanationContent) {
            explanationContent.innerHTML = result.html_preview;
          }
        }
        showSuccessMessage(result.message || 'AI generated a new explanation. Review and save when ready.');
        adminDebugLog('✅ [Rewrite AI] Unified explanation hydrated into editor.');
      } else {
        const message = (result && result.error) || 'Unable to regenerate the explanation. Please try again.';
        adminDebugLog(`❌ [Rewrite AI] API returned error: ${message}`, 'error');
        showWarningMessage(message);
      }
    } catch (error) {
      adminDebugLog(`❌ [Rewrite AI] Request failed: ${error}`, 'error');
      showWarningMessage(error.message || 'Rewrite service is temporarily unavailable. Please retry.');
    } finally {
      toggleButtonLoading(
        rewriteButton,
        false,
        '<i class="bi bi-magic"></i> Rewrite with AI'
      );
    }
  }

  /* -------------------------------------------------------------------------- */
  /* Event Wiring                                                               */
  /* -------------------------------------------------------------------------- */

  function bindEvents() {
    const questionBtn = document.querySelector('.js-ai-question');
    if (questionBtn) {
      questionBtn.addEventListener('click', handleQuestionAI);
    }

    const optionsBtn = document.querySelector('.js-ai-options');
    if (optionsBtn) {
      optionsBtn.addEventListener('click', handleOptionsAI);
    }

    const rewriteButton = document.getElementById('rewriteExplanationBtn');
    if (rewriteButton) {
      rewriteButton.addEventListener('click', handleRewriteExplanation);
    }

    const toggleEditModeBtn = document.getElementById('toggleEditMode');
    if (toggleEditModeBtn) {
      toggleEditModeBtn.addEventListener('click', function () {
        const editSections = ['questionEditSection', 'imageEditSection', 'optionsEditSection', 'explanationEditSection'];
        const isEditing = this.textContent.includes('Edit MCQ');

        editSections.forEach((id) => {
          const section = document.getElementById(id);
          if (section) {
            section.style.display = isEditing ? 'block' : 'none';
          }
        });

        if (isEditing) {
          this.innerHTML = '<i class="bi bi-x-circle"></i> Cancel Editing';
          this.classList.remove('btn-outline-info');
          this.classList.add('btn-outline-danger');
          adminDebugLog('✏️ Admin edit mode enabled.');
        } else {
          this.innerHTML = '<i class="bi bi-pencil"></i> Edit MCQ';
          this.classList.remove('btn-outline-danger');
          this.classList.add('btn-outline-info');
          adminDebugLog('✏️ Admin edit mode disabled.');
        }
      });
    }


    const focusToggle = document.getElementById('focusExplanationToggle');
    if (focusToggle) {
      focusToggle.addEventListener('click', () => {
        const focusActive = document.body.classList.toggle('explanation-focus-active');
        focusToggle.classList.toggle('btn-outline-secondary', !focusActive);
        focusToggle.classList.toggle('btn-primary', focusActive);
        focusToggle.innerHTML = focusActive
          ? '<i class="bi bi-eye-slash"></i> Exit focus mode'
          : '<i class="bi bi-eye"></i> Toggle focus mode';
      });
    }
  }

  function attachGlobalFunctions() {
    window.saveQuestion = saveQuestion;
    window.saveOptions = saveOptions;
    window.saveImage = saveImage;
    window.removeImage = removeImage;
    window.saveExplanation = saveExplanation;
    window.cancelEdit = cancelEdit;

    window.toggleExplanationRecording = function (button, textareaId) {
      if (!state.explanation.recorder) return;
      if (state.explanation.recorder.isRecording) {
        state.explanation.recorder.stop();
      } else {
        state.explanation.recorder.start(button, textareaId);
      }
    };

    window.showSuccessMessage = showSuccessMessage;
    window.showWarningMessage = showWarningMessage;
    window.toggleButtonLoading = toggleButtonLoading;
    window.renderQuestionDisplay = renderQuestionDisplay;
  }

  function cacheDomReferences() {
    state.question.textarea = document.getElementById('questionTextEdit');
    state.question.display = document.getElementById('questionDisplay');

    if (state.question.textarea) {
      state.question.originalText = state.question.textarea.value || '';
      state.question.lastSuccessfulText = state.question.originalText;
    }

    ['A', 'B', 'C', 'D'].forEach((letter) => {
      state.options.editors[letter] = document.getElementById(`option${letter}Edit`);
    });
    state.options.correctSelect = document.getElementById('correctAnswerEdit');

    state.explanation.textarea = document.getElementById('explanationUnifiedEdit');
    state.explanation.charCountEl = document.getElementById('explanationCharCount');
    state.explanation.recordStatusEl = document.getElementById('explanationRecordStatus');
    if (state.explanation.textarea) {
      state.explanation.lastSuccessfulText = state.explanation.textarea.value || '';
    }
  }

  /* -------------------------------------------------------------------------- */
  /* Public API                                                                 */
  /* -------------------------------------------------------------------------- */

  function init(config) {
    if (!config || !config.endpoints) {
      throw new Error('AIAdminEditor.init requires an endpoints configuration.');
    }

    state.mcqId = config.mcqId || null;
    state.csrfToken = getCookie('csrftoken');
    state.endpoints = Object.assign({}, config.endpoints);

    cacheDomReferences();
    setupInstructionModal();
    setupExplanationRecorder();
    bindEvents();
    attachGlobalFunctions();

    if (state.explanation.textarea) {
      if (typeof config.initialExplanationText === 'string') {
        state.explanation.textarea.value = config.initialExplanationText;
      }
      state.explanation.lastSuccessfulText = state.explanation.textarea.value || '';
      updateExplanationMetrics();
      state.explanation.textarea.addEventListener('input', updateExplanationMetrics);
    }

    const recordBtn = document.getElementById('explanationRecordBtn');
    if (recordBtn && state.explanation.recorder) {
      recordBtn.addEventListener('click', () => {
        const target = recordBtn.getAttribute('data-record-target');
        toggleExplanationRecording(recordBtn, target);
      });
    }

    if (!state.question.display && state.question.textarea) {
      renderQuestionDisplay(state.question.textarea.value);
    }

    adminDebugLog('✅ All systems ready - AI editing utilities initialized.');
  }

  window.AIAdminEditor = {
    init
  };
})(window, document);
