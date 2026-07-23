from flask import render_template, redirect, url_for, session, jsonify

from . import main_bp


@main_bp.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('index.html')


@main_bp.route('/health')
def health():
    return jsonify({"status": "ok"}), 200
