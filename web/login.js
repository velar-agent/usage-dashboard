// Login form: exchanges a token for a session cookie.
// 로그인 폼입니다. 토큰을 세션 쿠키로 바꿉니다.

const form = document.getElementById('login-form');
const input = document.getElementById('token');
const button = document.getElementById('submit');
const error = document.getElementById('error');

function showError(message) {
  error.textContent = message;
  error.hidden = false;
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  error.hidden = true;
  button.disabled = true;
  try {
    const response = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: input.value }),
    });
    if (response.ok) {
      location.assign('/');
      return;
    }
    if (response.status === 401) {
      showError('토큰이 올바르지 않거나 폐기되었습니다. 관리자에게 확인하십시오.');
    } else {
      showError(`로그인하지 못했습니다. (HTTP ${response.status})`);
    }
  } catch {
    showError('서버에 연결하지 못했습니다. 잠시 후 다시 시도하십시오.');
  } finally {
    button.disabled = false;
    input.select();
  }
});
