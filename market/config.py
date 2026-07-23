import os


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'secret!')
    DATABASE = os.environ.get('DATABASE', 'market.db')
