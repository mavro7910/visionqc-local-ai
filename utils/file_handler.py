# utils/file_handler.py
from PyQt5.QtWidgets import QFileDialog
import base64
import os

def get_image_file():
    path, _ = QFileDialog.getOpenFileName(
        None, '이미지 선택', '', 'Images (*.png *.jpg *.jpeg *.webp)'
    )
    return path