from flask import Blueprint

main_bp = Blueprint('main', __name__)

# routes 임포트는 main_bp 생성 이후에 위치해야 합니다
# (순환 import 방지: routes.py → market.main → routes.py 방지)
from market.main import routes  # noqa: E402, F401
