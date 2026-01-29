import hashlib
from dotenv import load_dotenv
import requests
import os
import urllib3

load_dotenv()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class IikoService:
    def __init__(self):
        self.server_url = os.getenv("IIKO_RESTO_URL")
        self.login = os.getenv("IIKO_LOGIN")
        self.password = os.getenv("IIKO_PASSWORD")
        self.token = None

    def _authenticate(self):
        """Получает новый токен авторизации"""
        if not self.server_url or not self.login:
            raise ValueError("Не настроены переменные окружения (IIKO_RESTO_URL, IIKO_LOGIN)")

        password_hash = hashlib.sha1(self.password.encode('utf-8')).hexdigest()
        url = f"{self.server_url}/resto/api/auth"
        params = {
            "login": self.login,
            "pass": password_hash
        }
        
        try:
            response = requests.post(url, data=params, verify=False)
            if response.status_code == 200:
                self.token = response.text
                print(f"🔑 Токен обновлен: {self.token[:10]}...")
            else:
                print(f"Ошибка авторизации: {response.status_code} {response.text}")
                self.token = None
                raise ConnectionError("Не удалось получить токен")
        except Exception as e:
            print(f"Ошибка соединения при авторизации: {e}")
            raise

    def get_token(self):
        if not self.token:
            self._authenticate()
        return self.token

    def request(self, method, endpoint, **kwargs):
        """Обертка над requests, которая обновляет токен при 401 ошибке"""
        if not self.token:
            self._authenticate()
        
        url = f"{self.server_url}{endpoint}"
        
        # Добавляем токен в параметры
        params = kwargs.get('params', {})
        params['key'] = self.token
        kwargs['params'] = params
        kwargs.setdefault('verify', False)

        response = requests.request(method, url, **kwargs)

        # Если токен протух (401), обновляем и пробуем снова
        if response.status_code == 401:
            print("⚠️ Токен истек. Обновление...")
            self._authenticate()
            kwargs['params']['key'] = self.token
            response = requests.request(method, url, **kwargs)
        
        return response

# Создаем экземпляр сервиса для импорта в другие файлы
iiko_service = IikoService()

if __name__ == "__main__":
    try:
        print(f"Текущий токен: {iiko_service.get_token()}")
    except Exception as e:
        print(f"Ошибка: {e}")
