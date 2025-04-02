# -*- coding: utf-8 -*-
import sys
import os
import time
import configparser
import re
import mimetypes
from typing import Union, Dict, List, Optional, Tuple, Any
import json

# --- PySide6 Imports ---
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QTextEdit, QPushButton, QComboBox, QListWidget, QFileDialog,
    QMessageBox, QGroupBox, QFrame, QStackedWidget, QDoubleSpinBox
)
from PySide6.QtCore import Qt, QObject, Signal, QThread, Slot, QTimer, QSize
from PySide6.QtGui import QFont, QColor # Removed unused Palette, Icon, Pixmap

# --- AI Imports ---
try:
    import google.generativeai as genai
    # Use ContentDict for history structure
    from google.generativeai.types import GenerationConfigDict, File, ContentDict, HarmCategory, HarmBlockThreshold
    from google.generativeai.generative_models import GenerativeModel, ChatSession # Import ChatSession
    import google.api_core.exceptions
    gemini_imported = True
except ImportError:
    # ... (error handling as before) ...
    gemini_imported = False
except Exception as e:
    # ... (error handling as before) ...
    gemini_imported = False; sys.exit(1)

# --- Constants ---
CONFIG_FILE = 'konenako_simple_config.ini'
DEFAULT_MODEL = 'gemini-1.5-flash-latest' # Use latest flash
DEFAULT_LANGUAGE = 'Finnish'
LANGUAGES = ['Finnish', 'English']
DEFAULT_TEMPERATURE = 0.6 # Slightly higher for more natural convo?
# MUISTILAPPU_BASENAME = "konenako_muistilappu_v2.md" # May not be needed explicitly if just another doc

# --- Styling (Optional) ---
APP_BG_COLOR = "#F0F4F8"; CHAT_BG = "#FFFFFF"; INPUT_BG="#FFFFFF"; STATUS_BG="#E3EAF1";
AI_MSG_COLOR = "#263238"; USER_MSG_COLOR = "#00695C"; SYSTEM_MSG_COLOR="#546E7A"; ERROR_MSG_COLOR="#D32F2F";
BUTTON_COLOR = "#1E88E5"; BUTTON_HOVER_COLOR = "#1565C0"; BUTTON_TEXT_COLOR = "#FFFFFF"; FONT_FAMILY = "Segoe UI"

# Role constants for chat history
ROLE_AI = "model"
ROLE_USER = "user"
ROLE_SYSTEM = "system" # For internal status messages in chat UI

# ==================================
# Worker Objects (Modified for Chat)
# ==================================
class AIChatWorker(QObject):
    result_ready = Signal(str) # Only sends back the AI text response
    error_occurred = Signal(str)
    # progress_update = Signal(str) # Less critical in simple chat

    # Takes the chat session and the new user message text
    def __init__(self, chat_session: Optional[ChatSession], user_message_text: str, doc_ref: Optional[File]):
        super().__init__()
        self.chat_session = chat_session
        self.user_message_text = user_message_text
        self.doc_ref = doc_ref # Pass doc reference if needed (e.g., for sending files with message)
        self._is_running = True

    @Slot()
    def run(self):
        if not self._is_running: return
        if not self.chat_session:
            self.error_occurred.emit("AI Chat session not initialized.")
            return

        try:
            print(f"Sending to AI: {self.user_message_text[:100]}...")
            # Construct the message content - can include files if needed
            # For Flash, sending the file with *every* message might be inefficient.
            # Better to rely on the File API URI being processed initially.
            # Check Gemini API docs for best practices with File API + Chat.
            # Assuming file is processed and AI can access via URI implicitly:
            message_content = [self.user_message_text]
            # Example if sending file explicitly is needed:
            # if self.doc_ref: message_content.append(self.doc_ref)

            # Send message using the existing chat session
            response = self.chat_session.send_message(message_content)

            if not self._is_running: return # Check if stopped during API call

            response_text = response.text # Simplified access for chat
            print(f"AI Response: {response_text[:200]}...")
            if self._is_running:
                self.result_ready.emit(response_text)

        except (google.api_core.exceptions.GoogleAPIError, ConnectionError, ValueError) as e:
            print(f"Error in AI Chat Worker: {e}")
            if self._is_running: self.error_occurred.emit(f"AI Error: {e}")
        except Exception as e:
            print(f"Unexpected Error in AI Chat Worker: {e}"); import traceback; traceback.print_exc()
            if self._is_running: self.error_occurred.emit(f"Unexpected Error: {e}")

    def stop(self):
        print("AIChatWorker stop() called.")
        self._is_running = False

class PDFUploadWorker(QObject):
    # (Largely unchanged - Keep robust error handling & progress reporting)
    finished = Signal(list) # List[File]
    error_occurred = Signal(str)
    progress_update = Signal(str) # Filename being processed
    file_processed = Signal(str) # Filename successfully processed by backend

    def __init__(self, file_paths: List[str], basenames: List[str]):
        super().__init__(); self.file_paths = file_paths; self.basenames = basenames; self._is_running = True

    @Slot()
    def run(self):
        if not self._is_running: self.error_occurred.emit("Upload stopped."); return
        temp_uploaded_references: List[File] = []
        try:
            print("Starting File API Upload..."); time.sleep(0.1)
            for i, file_path in enumerate(self.file_paths):
                if not self._is_running: print(f"UploadWorker: Stopping before processing idx {i}."); break
                filename = self.basenames[i]
                print(f"-> Uploading {filename} ({i+1}/{len(self.file_paths)})...")
                self.progress_update.emit(f"Uploading {filename}...")

                mime_type, _ = mimetypes.guess_type(file_path)
                if not mime_type: mime_type = 'application/octet-stream' # Fallback
                print(f"   MIME Type: {mime_type}")

                # Check if file still exists right before upload
                if not os.path.exists(file_path):
                    raise FileNotFoundError(f"File disappeared before upload: {filename}")

                uploaded_file = genai.upload_file(path=file_path, display_name=filename, mime_type=mime_type)
                if not self._is_running: break # Check immediately after upload call starts

                print(f"   '{filename}' upload initiated, waiting for processing...")
                self.progress_update.emit(f"Processing {filename}...")

                # --- File Processing Wait Loop ---
                start_time = time.time()
                wait_time = 5 # Initial wait
                max_wait = 120 # Max wait 2 minutes per file
                while time.time() - start_time < max_wait:
                    if not self._is_running: break # Check if stopped

                    try:
                        uploaded_file = genai.get_file(uploaded_file.name)
                    except Exception as get_err:
                        print(f"   Error checking file status for {filename}: {get_err}. Retrying in {wait_time}s...")
                        # Add exponential backoff? For now, just fixed wait.
                        time.sleep(wait_time); continue # Retry get_file

                    state = uploaded_file.state.name
                    print(f"   '{filename}' state: {state} (Elapsed: {time.time() - start_time:.1f}s)")

                    if state == 'ACTIVE':
                        self.file_processed.emit(filename)
                        temp_uploaded_references.append(uploaded_file)
                        print(f"<- Processed '{filename}'. URI: {uploaded_file.uri}")
                        break # Success for this file
                    elif state == 'FAILED':
                         print(f"Error: '{filename}' failed processing. State: {state}")
                         # Attempt deletion of failed file
                         try: genai.delete_file(uploaded_file.name)
                         except Exception as del_e: print(f"   Warning: Failed to delete failed file '{filename}': {del_e}")
                         raise google.api_core.exceptions.GoogleAPIError(f"File processing failed for '{filename}': State={state}")
                    elif state == 'PROCESSING':
                        if not self._is_running: break # Check again before sleep
                        time.sleep(wait_time)
                        wait_time = min(wait_time * 1.5, 15) # Increase wait time slightly
                    else: # Should not happen (e.g., DELETED?)
                         print(f"Error: Unexpected file state '{state}' for '{filename}'.")
                         raise google.api_core.exceptions.GoogleAPIError(f"Unexpected state for '{filename}': {state}")

                else: # Loop finished due to timeout
                     print(f"Error: Timeout waiting for '{filename}' to become ACTIVE.")
                     # Attempt deletion of timed-out file
                     try: genai.delete_file(uploaded_file.name)
                     except Exception as del_e: print(f"   Warning: Failed to delete timed-out file '{filename}': {del_e}")
                     raise TimeoutError(f"Timeout processing file '{filename}'")

                if not self._is_running: break # Exit outer loop if stopped

            # --- Loop Finished ---
            if self._is_running:
                 # Check if all requested files were processed successfully
                 if len(temp_uploaded_references) == len(self.file_paths):
                     print(f"File Upload finished. Emitting {len(temp_uploaded_references)} references.");
                     self.finished.emit(temp_uploaded_references)
                 else:
                     # Some files might have failed, or the process was stopped
                     failed_count = len(self.file_paths) - len(temp_uploaded_references)
                     print(f"Upload finished, but {failed_count} files were not successfully processed.")
                     # Decide whether to emit partial list or error? Let's emit partial for now.
                     self.finished.emit(temp_uploaded_references) # Emit refs for successful ones
                     # Error handling/reporting should happen in the main thread based on this partial list
            else:
                 print("Upload process was stopped. Not emitting 'finished'.")
                 # Clean up files uploaded before stop, if desired (optional, requires tracking)
                 # Example: self._cleanup_files(temp_uploaded_references)

        except (google.api_core.exceptions.GoogleAPIError, FileNotFoundError, TimeoutError, ValueError) as e:
             print(f"Error during File Upload: {e}"); self.error_occurred.emit(f"Upload Error: {e}")
        except Exception as e:
             print(f"Unexpected Error in PDF Upload: {e}"); self.error_occurred.emit(f"Unexpected Upload Error: {e}")

    def stop(self):
        print("PDFUploadWorker stop() called."); self._is_running = False

# ==================================
# Settings Widget (Simplified)
# ==================================
class SettingsWidget(QWidget):
    settings_applied = Signal(dict) # Emit settings dict
    upload_requested = Signal()
    files_added = Signal(list) # List of paths
    files_removed = Signal(list) # List of basenames
    files_cleared = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self); layout.setContentsMargins(15,15,15,15); layout.setSpacing(10)

        # Back button
        top_layout = QHBoxLayout()
        self.back_button = QPushButton("⬅️ Back to Chat")
        self.back_button.setObjectName("BackButton") # For styling
        top_layout.addWidget(self.back_button)
        top_layout.addStretch()
        layout.addLayout(top_layout)

        title_label = QLabel("⚙️ Settings"); title_font = QFont(FONT_FAMILY, 16, QFont.Weight.Bold); title_label.setFont(title_font); title_label.setAlignment(Qt.AlignmentFlag.AlignCenter); layout.addWidget(title_label)

        # --- AI Config ---
        ai_groupbox = QGroupBox("🤖 AI Configuration")
        ai_layout = QFormLayout(ai_groupbox)
        self.api_key_input = QLineEdit(); self.api_key_input.setEchoMode(QLineEdit.Password)
        ai_layout.addRow("Gemini API Key:", self.api_key_input)
        self.model_input = QLineEdit(DEFAULT_MODEL)
        ai_layout.addRow("Model Name:", self.model_input)
        self.language_combo = QComboBox(); self.language_combo.addItems(LANGUAGES)
        ai_layout.addRow("Tutor Language:", self.language_combo)
        self.temperature_spinbox = QDoubleSpinBox(); self.temperature_spinbox.setRange(0.0, 1.0); self.temperature_spinbox.setSingleStep(0.1); self.temperature_spinbox.setValue(DEFAULT_TEMPERATURE); self.temperature_spinbox.setToolTip("Controls randomness (0=deterministic, 1=more creative)")
        ai_layout.addRow("AI Temperature:", self.temperature_spinbox)
        layout.addWidget(ai_groupbox)

        # --- Course Material ---
        pdf_groupbox = QGroupBox("📚 Course Material (PDF, MD)")
        pdf_layout = QVBoxLayout(pdf_groupbox)
        self.pdf_list_widget = QListWidget(); self.pdf_list_widget.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        pdf_layout.addWidget(self.pdf_list_widget)
        pdf_button_layout = QHBoxLayout(); pdf_button_layout.setSpacing(5)
        self.add_files_button = QPushButton("➕ Add File(s)")
        self.remove_files_button = QPushButton("➖ Remove Selected")
        self.clear_files_button = QPushButton("❌ Clear All")
        self.upload_button = QPushButton("☁️ Upload to AI")
        self.add_files_button.clicked.connect(self._request_add_files)
        self.remove_files_button.clicked.connect(self._request_remove_files)
        self.clear_files_button.clicked.connect(self._request_clear_files)
        self.upload_button.clicked.connect(self.upload_requested) # Signal main window
        pdf_button_layout.addWidget(self.add_files_button)
        pdf_button_layout.addWidget(self.remove_files_button)
        pdf_button_layout.addWidget(self.clear_files_button)
        pdf_button_layout.addStretch()
        pdf_button_layout.addWidget(self.upload_button)
        pdf_layout.addLayout(pdf_button_layout)
        layout.addWidget(pdf_groupbox, 1) # Allow stretching

        # Apply Button (Save settings to config)
        self.apply_button = QPushButton("💾 Apply & Save Settings")
        self.apply_button.clicked.connect(self._emit_settings)
        layout.addWidget(self.apply_button)

        # Status Label
        self.status_label = QLabel("Status: Initializing...")
        self.status_label.setStyleSheet(f"color: {SYSTEM_MSG_COLOR}; padding: 5px;")
        layout.addWidget(self.status_label)

    def _request_add_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Select Course Files", "", "Supported Files (*.pdf *.md)");
        if files: self.files_added.emit(files)

    def _request_remove_files(self):
        items = self.pdf_list_widget.selectedItems()
        if items: self.files_removed.emit([i.text() for i in items])

    def _request_clear_files(self):
        if self.pdf_list_widget.count() > 0:
             reply = QMessageBox.question(self, "Confirm Clear", "Remove all files from the list?\n(Uploaded files will also be deleted from the AI backend if possible).", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No);
             if reply == QMessageBox.StandardButton.Yes: self.files_cleared.emit()
        else: QMessageBox.information(self, "Info", "File list is already empty.")

    def update_file_list(self, basenames: List[str]):
        self.pdf_list_widget.clear()
        self.pdf_list_widget.addItems(basenames)

    def _emit_settings(self):
        settings = {
            'api_key': self.api_key_input.text().strip(),
            'model': self.model_input.text().strip(),
            'language': self.language_combo.currentText(),
            'temperature': self.temperature_spinbox.value()
        }
        self.settings_applied.emit(settings)

    def load_settings(self, config_data: Dict[str, Any]):
        self.api_key_input.setText(config_data.get('api_key', ''))
        self.model_input.setText(config_data.get('model', DEFAULT_MODEL))
        self.language_combo.setCurrentText(config_data.get('language', DEFAULT_LANGUAGE))
        self.temperature_spinbox.setValue(config_data.get('temperature', DEFAULT_TEMPERATURE))

    def set_status(self, message: str, is_error: bool = False):
        self.status_label.setText(f"Status: {message}")
        color = ERROR_MSG_COLOR if is_error else SYSTEM_MSG_COLOR
        self.status_label.setStyleSheet(f"color: {color}; padding: 5px; background-color: {STATUS_BG}; border-radius: 3px;")

    def set_controls_enabled(self, enabled: bool):
        # Enable/disable all controls during processing
        for w in [self.api_key_input, self.model_input, self.language_combo,
                  self.temperature_spinbox, self.apply_button, self.pdf_list_widget,
                  self.add_files_button, self.remove_files_button, self.clear_files_button,
                  self.upload_button, self.back_button]:
            w.setEnabled(enabled)

# ==================================
# Chat Widget (Replaces CourseViewWidget)
# ==================================
class ChatWidget(QWidget):
    send_message_requested = Signal(str) # User message text

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self); layout.setContentsMargins(5,5,5,5); layout.setSpacing(5) # Reduced margins

        # Document Selector
        doc_layout = QHBoxLayout()
        doc_layout.addWidget(QLabel("Focus Document:"))
        self.doc_selector_combo = QComboBox()
        self.doc_selector_combo.setToolTip("Select the course document to discuss with the AI")
        doc_layout.addWidget(self.doc_selector_combo, 1) # Allow stretching
        layout.addLayout(doc_layout)

        # Chat Display
        self.chat_display = QTextEdit()
        self.chat_display.setReadOnly(True)
        self.chat_display.setStyleSheet(f"background-color: {CHAT_BG}; border: 1px solid #ccc; border-radius: 3px;")
        layout.addWidget(self.chat_display, 1) # Allow stretching

        # Input Area
        input_layout = QHBoxLayout(); input_layout.setSpacing(5)
        self.user_input_entry = QLineEdit()
        self.user_input_entry.setPlaceholderText("Type your message or question here...")
        self.user_input_entry.setStyleSheet(f"background-color: {INPUT_BG}; border: 1px solid #ccc; border-radius: 3px; padding: 8px;")
        self.send_button = QPushButton("Send")
        self.send_button.setObjectName("SendButton") # For styling
        self.send_button.setToolTip("Send message to the AI Tutor")

        input_layout.addWidget(self.user_input_entry, 1)
        input_layout.addWidget(self.send_button)
        layout.addLayout(input_layout)

        # Connections
        self.send_button.clicked.connect(self._emit_input)
        self.user_input_entry.returnPressed.connect(self._emit_input)
        # ComboBox signal will be connected in MainWindow

    def _emit_input(self):
        input_text = self.user_input_entry.text().strip()
        if input_text:
            self.send_message_requested.emit(input_text)
            self.user_input_entry.clear()

    def update_document_list(self, doc_basenames: List[str]):
        """Populates the document selector, preserving current selection if possible."""
        current_selection = self.doc_selector_combo.currentText()
        self.doc_selector_combo.blockSignals(True) # Prevent triggering signal during update
        self.doc_selector_combo.clear()
        self.doc_selector_combo.addItems(doc_basenames)
        # Try to restore selection
        index = self.doc_selector_combo.findText(current_selection)
        if index != -1:
             self.doc_selector_combo.setCurrentIndex(index)
        elif doc_basenames: # Select first item if previous is gone
            self.doc_selector_combo.setCurrentIndex(0)

        self.doc_selector_combo.blockSignals(False)


    def add_message(self, sender_role: str, text: str):
        """Adds a message to the chat display with appropriate styling."""
        # Basic HTML escaping for safety
        text_escaped = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('\n', '<br>')

        prefix = ""
        color = "#000000" # Default

        if sender_role == ROLE_AI:
            prefix = "🤖 AI Tutor:"
            color = AI_MSG_COLOR
        elif sender_role == ROLE_USER:
             prefix = "👤 You:"
             color = USER_MSG_COLOR
        elif sender_role == ROLE_SYSTEM:
             prefix = "ℹ️ System:"
             color = SYSTEM_MSG_COLOR
             text_escaped = f"<i>{text_escaped}</i>" # Italicize system messages
        else: # Fallback
            prefix = f"{sender_role}:"

        formatted_message = f'<div style="margin-bottom: 8px;"><b style="color:{color};">{prefix}</b><br><span style="color:{color};">{text_escaped}</span></div>'
        self.chat_display.append(formatted_message)
        self.chat_display.verticalScrollBar().setValue(self.chat_display.verticalScrollBar().maximum()) # Auto-scroll

    def clear_chat(self):
        self.chat_display.clear()

    def set_input_enabled(self, enabled: bool):
        self.user_input_entry.setEnabled(enabled)
        self.send_button.setEnabled(enabled)
        self.doc_selector_combo.setEnabled(enabled) # Allow changing doc only when idle

    def get_selected_document(self) -> Optional[str]:
        return self.doc_selector_combo.currentText() if self.doc_selector_combo.count() > 0 else None

# ==================================
# Main Application Window (Simplified)
# ==================================
class SimpleTutorApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Konenäkö Tutor (Simple)")
        self.setGeometry(100, 100, 750, 650) # Smaller default size?

        # --- State ---
        self.config: Dict[str, Any] = {} # Store loaded config (api_key, model, etc.)
        self.local_file_paths: Dict[str, str] = {} # {basename: full_path}
        self.uploaded_files: Dict[str, File] = {} # {basename: FileAPI_Object} - Files ACTIVE on backend

        self.model: Optional[GenerativeModel] = None
        self.current_chat_session: Optional[ChatSession] = None
        self.current_chat_history: List[ContentDict] = []
        self.selected_doc_basename: Optional[str] = None
        self.is_ai_configured = False
        self.is_processing = False # General flag for background tasks

        self.thread: Optional[QThread] = None
        self.worker: Optional[Union[AIChatWorker, PDFUploadWorker]] = None

        # --- UI ---
        self.central_widget = QWidget()
        self.main_layout = QVBoxLayout(self.central_widget)
        self.main_layout.setContentsMargins(0,0,0,0); self.main_layout.setSpacing(0)
        self.setCentralWidget(self.central_widget)

        self._create_header() # Simple header

        self.view_stack = QStackedWidget()
        self.chat_widget = ChatWidget()
        self.settings_widget = SettingsWidget()
        self.view_stack.addWidget(self.chat_widget)    # Index 0
        self.view_stack.addWidget(self.settings_widget) # Index 1
        self.main_layout.addWidget(self.view_stack, 1) # Allow view stack to stretch

        self._create_status_bar()

        # --- Connections ---
        self.settings_button.clicked.connect(self.show_settings_view)
        self.settings_widget.back_button.clicked.connect(self.show_chat_view)
        self.settings_widget.settings_applied.connect(self.apply_and_save_settings)
        self.settings_widget.files_added.connect(self.add_files)
        self.settings_widget.files_removed.connect(self.remove_files)
        self.settings_widget.files_cleared.connect(self.clear_all_files)
        self.settings_widget.upload_requested.connect(self.upload_files_to_ai)

        self.chat_widget.send_message_requested.connect(self.handle_user_message)
        self.chat_widget.doc_selector_combo.currentTextChanged.connect(self.handle_document_selection_change)

        # --- Initialization ---
        self.apply_stylesheet() # Apply basic styling
        self.load_config()      # Load API key, settings
        self._update_ui_state() # Populate file lists etc.
        self.show_chat_view()   # Start on chat view
        if self.config.get('api_key'):
            QTimer.singleShot(100, self.initialize_ai) # Try configuring AI early if key exists
        else:
             self.update_status("AI not configured. Please add API Key in Settings.", is_error=True)

    def _create_header(self):
        self.header_widget = QFrame(); self.header_widget.setObjectName("Header")
        self.header_widget.setStyleSheet(f"background-color: {APP_BG_COLOR}; border-bottom: 1px solid #ccc; padding: 5px;")
        header_layout = QHBoxLayout(self.header_widget); header_layout.setContentsMargins(10, 5, 10, 5)
        title = QLabel("Konenäkö Tutor"); title.setFont(QFont(FONT_FAMILY, 12, QFont.Weight.Bold))
        header_layout.addWidget(title); header_layout.addStretch()
        self.settings_button = QPushButton("⚙️ Settings"); self.settings_button.setFixedSize(QSize(100, 30))
        self.settings_button.setObjectName("SettingsButton")
        header_layout.addWidget(self.settings_button)
        self.main_layout.addWidget(self.header_widget)

    def _create_status_bar(self):
        self.status_bar_label = QLabel("Initializing...")
        self.status_bar_label.setStyleSheet(f"background-color: {STATUS_BG}; padding: 5px 10px; color: {SYSTEM_MSG_COLOR};")
        self.main_layout.addWidget(self.status_bar_label)

    def update_status(self, message: str, is_error: bool = False, processing: bool = False):
        """Updates the status bar and settings status label."""
        self.is_processing = processing # Update internal flag

        status_prefix = "⏳" if processing else ("❌" if is_error else "ℹ️")
        full_message = f"{status_prefix} {message}"
        self.status_bar_label.setText(full_message)
        color = ERROR_MSG_COLOR if is_error else SYSTEM_MSG_COLOR
        self.status_bar_label.setStyleSheet(f"background-color: {STATUS_BG}; padding: 5px 10px; color: {color};")

        # Update settings status too
        self.settings_widget.set_status(message, is_error)

        # Enable/disable controls based on processing state
        self.set_controls_enabled(not processing)
        QApplication.processEvents() # Ensure UI updates immediately


    def set_controls_enabled(self, enabled: bool):
        """Enable/disable relevant controls during processing."""
        self.chat_widget.set_input_enabled(enabled)
        self.settings_widget.set_controls_enabled(enabled)
        self.settings_button.setEnabled(enabled)
        # Keep upload button enabled status tied to having files, handled in _update_ui_state

    def apply_stylesheet(self):
        # Apply simple QSS
        self.setStyleSheet(f"""
            QMainWindow {{ background-color: {APP_BG_COLOR}; }}
            QGroupBox {{ font-weight: bold; border: 1px solid #ccc; border-radius: 4px; margin-top: 10px; padding: 10px; }}
            QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; padding: 0 5px; left: 10px; }}
            QPushButton {{ background-color: #E0E0E0; border: 1px solid #bbb; border-radius: 3px; padding: 5px 10px; min-height: 20px;}}
            QPushButton:hover {{ background-color: #d0d0d0; border: 1px solid #999; }}
            QPushButton:disabled {{ background-color: #f5f5f5; color: #aaa; }}
            #SendButton, #UploadButton, #ApplyButton {{ background-color: {BUTTON_COLOR}; color: {BUTTON_TEXT_COLOR}; border: none; }}
            #SendButton:hover, #UploadButton:hover, #ApplyButton:hover {{ background-color: {BUTTON_HOVER_COLOR}; }}
            #BackButton {{ /* Custom style if needed */ }}
            QLineEdit, QComboBox, QDoubleSpinBox, QTextEdit {{ border: 1px solid #ccc; border-radius: 3px; padding: 5px; background-color: #fff; }}
            QListWidget {{ border: 1px solid #ccc; border-radius: 3px; background-color: #fff; }}
        """)

    # --- View Management ---
    def show_chat_view(self):
        self.view_stack.setCurrentIndex(0)
        self._update_ui_state() # Ensure doc selector is up-to-date

    def show_settings_view(self):
        self.settings_widget.load_settings(self.config) # Load current config into view
        self.settings_widget.update_file_list(list(self.local_file_paths.keys()))
        self.update_status("Viewing Settings.") # Update status via main method
        self.view_stack.setCurrentIndex(1)

    # --- Config Management ---
    def load_config(self):
        """Loads settings from the INI file."""
        config = configparser.ConfigParser()
        defaults = {
            'api_key': '', 'model': DEFAULT_MODEL, 'language': DEFAULT_LANGUAGE,
            'temperature': str(DEFAULT_TEMPERATURE), 'files': '{}' # Store file paths as JSON dict
        }
        if os.path.exists(CONFIG_FILE):
            try:
                config.read(CONFIG_FILE)
                self.config['api_key'] = config.get('Settings', 'APIKey', fallback=defaults['api_key'])
                self.config['model'] = config.get('Settings', 'Model', fallback=defaults['model'])
                self.config['language'] = config.get('Settings', 'Language', fallback=defaults['language'])
                self.config['temperature'] = config.getfloat('Settings', 'Temperature', fallback=float(defaults['temperature']))
                # Load file paths
                files_json = config.get('Files', 'LocalPaths', fallback=defaults['files'])
                self.local_file_paths = json.loads(files_json)

            except (configparser.Error, json.JSONDecodeError, ValueError, KeyError) as e:
                print(f"Error loading config: {e}. Using defaults.")
                self.config = {k: (float(v) if k=='temperature' else v) for k,v in defaults.items()}
                self.config['files'] = json.loads(defaults['files']) # Ensure files is dict
                self.local_file_paths = {}
                # Optionally warn user about config reset
                QMessageBox.warning(self, "Config Load Error", f"Could not load settings from {CONFIG_FILE}. Using defaults.\nError: {e}")
        else:
             print("Config file not found, using defaults.")
             self.config = {k: (float(v) if k=='temperature' else v) for k,v in defaults.items()}
             self.local_file_paths = {}

        print(f"Config loaded: Model={self.config.get('model')}, Lang={self.config.get('language')}, Temp={self.config.get('temperature')}")
        print(f"Local file paths loaded: {len(self.local_file_paths)} files")


    def save_config(self):
        """Saves current settings and file paths to INI."""
        config = configparser.ConfigParser()
        config['Settings'] = {
            'APIKey': self.config.get('api_key', ''),
            'Model': self.config.get('model', DEFAULT_MODEL),
            'Language': self.config.get('language', DEFAULT_LANGUAGE),
            'Temperature': str(self.config.get('temperature', DEFAULT_TEMPERATURE))
        }
        config['Files'] = {
            'LocalPaths': json.dumps(self.local_file_paths or {})
        }
        try:
            with open(CONFIG_FILE, 'w') as configfile:
                config.write(configfile)
            print("Config saved.")
            return True
        except Exception as e:
            print(f"Error saving config: {e}")
            QMessageBox.critical(self, "Config Save Error", f"Could not write settings to {CONFIG_FILE}.\nError: {e}")
            return False

    @Slot(dict)
    def apply_and_save_settings(self, settings: Dict[str, Any]):
        """Applies settings from the widget, saves, and reconfigures AI if needed."""
        if self.is_processing:
            QMessageBox.warning(self, "Busy", "Cannot change settings while a task is running."); return

        print("Applying settings:", settings)
        old_api_key = self.config.get('api_key')
        old_model = self.config.get('model')

        # Update internal config state
        self.config.update(settings)

        # Save updated config to file
        if self.save_config():
             self.update_status("Settings saved.")
             # Check if AI reconfiguration is needed
             if settings.get('api_key') != old_api_key or settings.get('model') != old_model:
                 self.update_status("API Key or Model changed, re-initializing AI...")
                 self.initialize_ai() # Re-run AI setup
             # If only language/temp changed, the next chat session will use them
        else:
            self.update_status("Failed to save settings.", is_error=True)


    # --- AI Initialization ---
    def initialize_ai(self):
        """Configures the Gemini API and creates the GenerativeModel instance."""
        if not gemini_imported:
             self.update_status("Google AI library not installed.", is_error=True); return False
        api_key = self.config.get('api_key')
        model_name = self.config.get('model')
        if not api_key or not model_name:
             self.update_status("API Key or Model Name missing in settings.", is_error=True); return False

        self.update_status("Configuring AI...", processing=True)
        self.is_ai_configured = False
        self.model = None
        self.current_chat_session = None # Invalidate chat session

        try:
            genai.configure(api_key=api_key)
            # Safety settings (Example: block dangerous content)
            safety_settings = {
                 HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
                 HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
                 HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
                 HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
            }
            generation_config = GenerationConfigDict(temperature=self.config.get('temperature', DEFAULT_TEMPERATURE))

            self.model = GenerativeModel(
                model_name,
                generation_config=generation_config,
                safety_settings=safety_settings
                # system_instruction= # Can potentially be set here for some models/versions
            )

            self.is_ai_configured = True
            self.update_status(f"AI Configured: '{model_name}'. Ready.", processing=False)
            print(f"AI Configured: Model='{model_name}', Temp={self.config.get('temperature')}")
            # After successful config, check if we can start a chat
            self._try_start_chat_session()
            return True

        except (google.api_core.exceptions.PermissionDenied, google.api_core.exceptions.Unauthenticated) as auth_err:
             error_msg = f"Authentication Error: {auth_err}. Check API Key/Project."
             QMessageBox.critical(self, "AI Config Error", error_msg); self.update_status(error_msg, is_error=True, processing=False); return False
        except google.api_core.exceptions.NotFound:
             error_msg = f"Model '{model_name}' not found."
             QMessageBox.critical(self, "AI Config Error", error_msg); self.update_status(error_msg, is_error=True, processing=False); return False
        except Exception as e:
             error_msg = f"AI configuration failed: {e}";
             QMessageBox.critical(self, "AI Config Error", error_msg); print(error_msg); self.update_status(error_msg, is_error=True, processing=False); return False
        finally:
             # If init failed, ensure state reflects it
             if not self.is_ai_configured:
                 self.model = None
                 self.current_chat_session = None


    # --- File Management ---
    @Slot(list)
    def add_files(self, file_paths: List[str]):
        if self.is_processing: QMessageBox.warning(self, "Busy", "Cannot add files now."); return
        added = 0
        for path in file_paths:
            if not os.path.exists(path):
                 QMessageBox.warning(self, "File Not Found", f"Skipping missing file: {path}"); continue
            basename = os.path.basename(path)
            if basename not in self.local_file_paths:
                 self.local_file_paths[basename] = path
                 added += 1
            else:
                 QMessageBox.warning(self, "Duplicate File", f"'{basename}' is already in the list.")
        if added > 0:
            print(f"Added {added} files to local list.")
            self.save_config() # Save updated file list
            self._update_ui_state()
            self.update_status(f"Added {added} files. Please 'Upload to AI'.")

    @Slot(list)
    def remove_files(self, basenames_to_remove: List[str]):
        if self.is_processing: QMessageBox.warning(self, "Busy", "Cannot remove files now."); return
        removed_count = 0
        files_to_delete_backend = []

        for basename in basenames_to_remove:
            if basename in self.local_file_paths:
                del self.local_file_paths[basename]
                removed_count += 1
                print(f"Removed '{basename}' from local list.")
                if basename in self.uploaded_files:
                     files_to_delete_backend.append(self.uploaded_files[basename])
                     del self.uploaded_files[basename] # Remove from uploaded dict too

        if removed_count > 0:
            # If the currently selected doc was removed, reset selection/chat
            if self.selected_doc_basename in basenames_to_remove:
                print(f"Selected document '{self.selected_doc_basename}' was removed. Resetting chat.")
                self.selected_doc_basename = None
                self.current_chat_session = None
                self.current_chat_history = []
                self.chat_widget.clear_chat()

            self.save_config() # Save updated local list
            self._attempt_backend_deletion(files_to_delete_backend)
            self._update_ui_state()
            self.update_status(f"Removed {removed_count} files.")
            # If no docs left, update status
            if not self.local_file_paths:
                 self.update_status("No local files loaded.")
            elif not self.uploaded_files:
                 self.update_status("No files uploaded to AI. Please Upload.")


    @Slot()
    def clear_all_files(self):
        if self.is_processing: QMessageBox.warning(self, "Busy", "Cannot clear files now."); return
        if not self.local_file_paths: return # Already empty

        files_to_delete_backend = list(self.uploaded_files.values())
        self.local_file_paths = {}
        self.uploaded_files = {}
        self.selected_doc_basename = None
        self.current_chat_session = None
        self.current_chat_history = []
        self.chat_widget.clear_chat()
        print("Cleared all local files and active uploads.")

        self.save_config()
        self._attempt_backend_deletion(files_to_delete_backend)
        self._update_ui_state()
        self.update_status("Cleared all files.")

    def _attempt_backend_deletion(self, file_api_objects: List[File]):
        """Helper to delete files from Gemini backend."""
        if not file_api_objects: return
        if not self.is_ai_configured:
             print("Skipping backend deletion: AI not configured.")
             return

        print(f"Attempting backend deletion for {len(file_api_objects)} file(s)...")
        # Consider doing this in a thread if many files? For now, do it synchronously.
        deleted_count = 0
        failed_count = 0
        for file_obj in file_api_objects:
             if not file_obj or not hasattr(file_obj, 'name'): continue
             try:
                 genai.delete_file(file_obj.name)
                 print(f"  Deleted '{getattr(file_obj, 'display_name', file_obj.name)}' from backend.")
                 deleted_count += 1
             except Exception as e:
                 print(f"  Warning: Failed deleting '{getattr(file_obj, 'display_name', file_obj.name)}': {e}")
                 failed_count += 1
        if failed_count > 0:
             QMessageBox.warning(self, "Backend Deletion Issue", f"Failed to delete {failed_count} file(s) from the AI backend. They might need manual cleanup.")
        elif deleted_count > 0:
             print("Backend deletion completed.")


    # --- File Upload ---
    @Slot()
    def upload_files_to_ai(self):
        if self.is_processing: QMessageBox.warning(self, "Busy", "Already processing."); return
        if not self.is_ai_configured: QMessageBox.critical(self, "Error", "AI not configured."); return
        if not self.local_file_paths: QMessageBox.information(self, "No Files", "Add files to the list first."); return

        # Check which files need uploading (local list vs self.uploaded_files)
        files_to_upload_basenames = list(self.local_file_paths.keys() - self.uploaded_files.keys())
        paths_to_upload = [self.local_file_paths[b] for b in files_to_upload_basenames if b in self.local_file_paths]

        if not paths_to_upload:
             QMessageBox.information(self, "Up-to-date", "All local files appear to be already uploaded."); return

        # Check files exist before starting worker
        missing_files = [p for p in paths_to_upload if not os.path.exists(p)]
        if missing_files:
             QMessageBox.critical(self, "File Not Found", f"Cannot upload. Missing:\n" + "\n".join(missing_files)); return

        self.update_status(f"Starting upload for {len(paths_to_upload)} file(s)...", processing=True)

        self.thread = QThread(self)
        self.worker = PDFUploadWorker(paths_to_upload, files_to_upload_basenames)
        self.worker.moveToThread(self.thread)

        # Connect signals
        self.worker.progress_update.connect(lambda msg: self.update_status(msg, processing=True))
        # self.worker.file_processed.connect(...) # Can add detailed status later
        self.worker.finished.connect(self._handle_upload_finished)
        self.worker.error_occurred.connect(self._handle_upload_error)
        self.thread.started.connect(self.worker.run)
        # Cleanup connections
        self.worker.finished.connect(self.thread.quit)
        self.worker.error_occurred.connect(self.thread.quit)
        self.thread.finished.connect(self._on_thread_finished)
        self.thread.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)

        self.thread.start()

    @Slot(list)
    def _handle_upload_finished(self, uploaded_file_objects: List[File]):
        print(f"Upload worker finished. Received {len(uploaded_file_objects)} potential file objects.")
        newly_uploaded_count = 0
        if uploaded_file_objects:
            for file_obj in uploaded_file_objects:
                basename = getattr(file_obj, 'display_name', None)
                if basename and basename in self.local_file_paths: # Check if it's one we requested
                    self.uploaded_files[basename] = file_obj
                    newly_uploaded_count += 1
                else:
                    print(f"Warning: Received unexpected/invalid file object: {basename}. Ignoring.")
                    # Attempt to delete unexpected file?
                    if hasattr(file_obj, 'name'):
                         self._attempt_backend_deletion([file_obj])

        total_uploaded = len(self.uploaded_files)
        total_local = len(self.local_file_paths)
        if newly_uploaded_count > 0:
            self.update_status(f"Uploaded {newly_uploaded_count} file(s). Total active: {total_uploaded}/{total_local}.", processing=False)
        elif total_uploaded == total_local and total_local > 0:
             self.update_status(f"All {total_local} files are active on backend.", processing=False)
        else:
             # This case might happen if worker finished but processed 0 files successfully
             self.update_status(f"Upload finished, but issues occurred. Active: {total_uploaded}/{total_local}.", is_error=True, processing=False)

        self._update_ui_state()
        self._try_start_chat_session() # Try starting chat if possible now


    @Slot(str)
    def _handle_upload_error(self, error_message: str):
        print(f"Upload Error: {error_message}")
        QMessageBox.critical(self, "Upload Error", error_message)
        self.update_status(f"Upload failed: {error_message.split(':')[0]}", is_error=True, processing=False)
        # Keep existing uploaded files, user might retry


    # --- Chat Logic ---
    @Slot(str)
    def handle_document_selection_change(self, selected_basename: str):
        """Starts a new chat session when the user selects a different document."""
        if self.is_processing or not selected_basename or selected_basename == self.selected_doc_basename:
            return # Don't restart if busy, selection is empty, or hasn't changed

        if selected_basename not in self.uploaded_files:
            QMessageBox.warning(self, "File Not Uploaded", f"'{selected_basename}' has not been successfully uploaded to the AI. Please use 'Upload to AI' in Settings.")
            # Revert selection in combo box?
            self.chat_widget.doc_selector_combo.blockSignals(True)
            index = self.chat_widget.doc_selector_combo.findText(self.selected_doc_basename or "")
            self.chat_widget.doc_selector_combo.setCurrentIndex(index if index != -1 else 0)
            self.chat_widget.doc_selector_combo.blockSignals(False)
            return

        print(f"Document selection changed to: {selected_basename}")
        self.selected_doc_basename = selected_basename
        self.current_chat_session = None # Invalidate old session
        self.current_chat_history = [] # Clear history for new doc
        self.chat_widget.clear_chat()
        self.update_status(f"Starting chat for document: {selected_basename}...")

        # Start the new session (will trigger AI intro)
        self._try_start_chat_session()


    def _try_start_chat_session(self):
        """Attempts to start a new chat session if conditions are met."""
        if not self.is_ai_configured or not self.model:
            print("Cannot start chat: AI not configured.")
            # Status already set by initialize_ai usually
            return
        if not self.selected_doc_basename:
             # Try selecting the first available uploaded doc
             available_docs = list(self.uploaded_files.keys())
             if available_docs:
                 print("No document selected, defaulting to first uploaded:", available_docs[0])
                 # This will trigger handle_document_selection_change -> _try_start_chat_session again
                 self.chat_widget.doc_selector_combo.setCurrentText(available_docs[0])
             else:
                 print("Cannot start chat: No document selected or uploaded.")
                 self.update_status("Select a document (or add/upload files).")
             return
        if self.selected_doc_basename not in self.uploaded_files:
             print(f"Cannot start chat: Selected document '{self.selected_doc_basename}' not uploaded.")
             self.update_status(f"Error: '{self.selected_doc_basename}' not uploaded.", is_error=True)
             return

        if self.current_chat_session: # Already started for this doc
            print("Chat session already active.")
            self.update_status(f"Chatting about: {self.selected_doc_basename}")
            return

        # --- Start New Chat ---
        self.update_status(f"Initializing chat for {self.selected_doc_basename}...", processing=True)
        doc_file_obj = self.uploaded_files[self.selected_doc_basename]

        # Prepare system instruction
        system_prompt = self._build_system_prompt(self.selected_doc_basename)

        # Start the chat session (history is initially empty)
        # Depending on API/model, system prompt might go here or in first message
        try:
            # Option 1: System instruction in start_chat (if supported)
            # self.current_chat_session = self.model.start_chat(
            #      history=[],
            #      # system_instruction=system_prompt # Check documentation
            # )
            # Option 2: Prepend system instruction implicitly (common)
            self.current_chat_session = self.model.start_chat(history=[])
            print("Chat session started.")

            # Send initial message to AI (including system prompt if not set above)
            # We use the worker to handle the first turn
            initial_message_to_ai = f"{system_prompt}\n\nLet's begin. Please provide a brief introduction to the document '{self.selected_doc_basename}' or suggest a starting point for discussion."
            self._start_ai_chat_turn(initial_message_to_ai, is_initial_turn=True)


        except Exception as e:
             msg = f"Failed to start chat session: {e}"
             print(msg)
             QMessageBox.critical(self, "Chat Error", msg)
             self.update_status("Error starting chat.", is_error=True, processing=False)
             self.current_chat_session = None


    def _build_system_prompt(self, current_doc_name: str) -> str:
        """Builds the system prompt string."""
        language = self.config.get('language', DEFAULT_LANGUAGE)
        # Basic template - can be customized further
        prompt = (
            f"You are 'Konenäkö Tutor', an AI assistant for a Machine Vision course teaching in {language}.\n"
            f"Your goal is to help the student understand the concepts presented ONLY in the provided course document: '{current_doc_name}'.\n"
            f"The document is provided via the File API and you should have access to it.\n\n"
            "Based on our chat history and the document content, guide the student:\n"
            "- Explain concepts clearly.\n"
            "- Ask relevant questions.\n"
            "- Provide exercises when appropriate.\n"
            "- Offer constructive feedback.\n"
            "- Maintain a supportive, conversational tone.\n"
            "- Adapt based on the student's input.\n\n"
            "CRITICAL RULES:\n"
            "- Base ALL output STRICTLY on the provided document '{current_doc_name}'. If info isn't there, say so.\n"
            "- Use citations like '[Page X]' or '[Section Y]' when referencing the document.\n"
            "- Decide the conversational next step (explain, ask, exercise, etc.).\n"
            "- Wait for the student's input after asking a question or giving an exercise."
        )
        return prompt


    @Slot(str)
    def handle_user_message(self, user_text: str):
        """Handles input from the chat widget."""
        if self.is_processing: QMessageBox.warning(self, "Busy", "Please wait for the AI's response."); return
        if not self.current_chat_session:
            QMessageBox.warning(self, "No Chat Active", "Please select an uploaded document to start chatting."); return

        # Add user message to UI and history
        self.chat_widget.add_message(ROLE_USER, user_text)
        self.current_chat_history.append({'role': ROLE_USER, 'parts': [user_text]})

        # Trigger AI response
        self._start_ai_chat_turn(user_text)


    def _start_ai_chat_turn(self, message_for_ai: str, is_initial_turn: bool = False):
        """Starts the AIChatWorker to get the next response."""
        if not self.current_chat_session or not self.is_ai_configured: return

        self.update_status("AI is thinking...", processing=True)

        # Pass the current chat session and the latest user message text
        # The worker will use chat_session.send_message which handles history internally
        doc_ref = self.uploaded_files.get(self.selected_doc_basename)

        self.thread = QThread(self)
        # Pass the ChatSession object itself to the worker
        self.worker = AIChatWorker(self.current_chat_session, message_for_ai, doc_ref)
        self.worker.moveToThread(self.thread)

        # Connections
        self.worker.result_ready.connect(self._handle_ai_result)
        self.worker.error_occurred.connect(self._handle_ai_error)
        self.thread.started.connect(self.worker.run)
        # Cleanup
        self.worker.result_ready.connect(self.thread.quit)
        self.worker.error_occurred.connect(self.thread.quit)
        self.thread.finished.connect(self._on_thread_finished)
        self.thread.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)

        self.thread.start()


    @Slot(str)
    def _handle_ai_result(self, ai_response_text: str):
        """Handles the AI response text from the worker."""
        # Add AI response to UI and history
        self.chat_widget.add_message(ROLE_AI, ai_response_text)
        # The ChatSession object internally updates its history, but we might
        # want our own copy for potential future use (e.g. saving/loading chat)
        # Note: Gemini API might not expose history easily after send_message.
        # If we need to reconstruct history, store it manually here.
        self.current_chat_history.append({'role': ROLE_AI, 'parts': [ai_response_text]})

        self.update_status(f"Chatting about: {self.selected_doc_basename}", processing=False)


    @Slot(str)
    def _handle_ai_error(self, error_message: str):
        """Handles errors from the AIChatWorker."""
        print(f"AI Chat Error: {error_message}")
        self.chat_widget.add_message(ROLE_SYSTEM, f"Error: {error_message}") # Show error in chat
        self.update_status(f"AI Error: {error_message.split(':')[0]}", is_error=True, processing=False)


    # --- UI State Update ---
    def _update_ui_state(self):
        """Updates UI elements based on current application state."""
        # Update file list in settings
        local_basenames = list(self.local_file_paths.keys())
        self.settings_widget.update_file_list(local_basenames)

        # Update document selector in chat (use only *uploaded* files)
        uploaded_basenames = list(self.uploaded_files.keys())
        self.chat_widget.update_document_list(uploaded_basenames)

        # Enable/disable upload button
        can_upload = bool(self.local_file_paths) and self.is_ai_configured
        self.settings_widget.upload_button.setEnabled(can_upload)

        # Select current document if possible
        if self.selected_doc_basename and self.selected_doc_basename in uploaded_basenames:
             if self.chat_widget.doc_selector_combo.currentText() != self.selected_doc_basename:
                  self.chat_widget.doc_selector_combo.setCurrentText(self.selected_doc_basename)
        elif uploaded_basenames: # Select first available if none selected
            if self.chat_widget.doc_selector_combo.currentIndex() == -1:
                 self.chat_widget.doc_selector_combo.setCurrentIndex(0)
                 # Trigger selection change handler to start chat? Be careful of loops.
                 # It might be better to trigger _try_start_chat_session explicitly after UI update.
                 # QTimer.singleShot(0, lambda: self.handle_document_selection_change(uploaded_basenames[0]))


    # --- Thread Finish ---
    @Slot()
    def _on_thread_finished(self):
        """Called when AI or Upload thread finishes."""
        print("Background thread finished.")
        # Check if it was AI or Upload? Doesn't strictly matter for status update.
        if self.worker is not None: # Check if worker exists before clearing
             if isinstance(self.worker, AIChatWorker):
                 # Re-enable input after AI turn if no error occurred during processing
                 if not self.is_processing: # Check the flag which _handle_* methods should update
                     self.set_controls_enabled(True)
             elif isinstance(self.worker, PDFUploadWorker):
                  # Upload finished, controls already re-enabled by update_status
                  pass
        else: # Thread finished but worker was already cleared (e.g., during close)
             self.set_controls_enabled(True) # Ensure controls are enabled

        self.thread = None
        self.worker = None
        # Don't reset self.is_processing here, rely on update_status calls


    # --- Window Closing ---
    def closeEvent(self, event):
        """Handles window closing, saves config, stops threads."""
        if self.is_processing and self.thread and self.thread.isRunning():
            reply = QMessageBox.question(self, "Confirm Exit", "A background task is running. Quit anyway?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.Yes:
                print("Stopping background task on close...")
                if self.worker: self.worker.stop()
                self.thread.quit()
                if not self.thread.wait(1000): self.thread.terminate()
            else:
                event.ignore(); return

        self.save_config()
        # Optionally delete backend files on close? Could be annoying.
        # self._attempt_backend_deletion(list(self.uploaded_files.values()))
        event.accept()


# --- Main Execution ---
if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = SimpleTutorApp()
    window.show()
    sys.exit(app.exec())