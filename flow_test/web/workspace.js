/* Test marker and authentication only. All application screens use web/app.js. */
const testFragment = new URLSearchParams(location.hash.slice(1));
if (testFragment.get('token')) {
  sessionStorage.setItem('flowAdminToken', testFragment.get('token'));
  history.replaceState(null, '', location.pathname);
}
const marker = document.createElement('div');
marker.className = 'test-workspace-marker';
marker.textContent = 'TEST WORKSPACE · Fictional data · Zoho writes go to test-gokul only';
document.getElementById('mainContent').prepend(marker);

const testLogin = document.createElement('dialog');
testLogin.className = 'test-workspace-login';
testLogin.innerHTML = `<form id="testLoginForm"><h2>Open test workspace</h2><p>Use the test admin key to access the fictional records.</p><label>Test admin key<input id="testAdminKey" type="password" autocomplete="off" required></label><p id="testLoginError" role="alert"></p><button class="primary-button" type="submit">Open test workspace</button></form>`;
document.body.append(testLogin);
function showTestLogin() { if (!testLogin.open) testLogin.showModal(); }
document.addEventListener('test-workspace-login', showTestLogin);
testLogin.addEventListener('cancel', event => event.preventDefault());
document.getElementById('testLoginForm').onsubmit = async event => {
  event.preventDefault();
  const key = document.getElementById('testAdminKey').value.trim();
  const error = document.getElementById('testLoginError');
  try {
    const response = await fetch('/test/api/overview', {headers:{Authorization:`Bearer ${key}`}});
    if (!response.ok) throw new Error('The admin key was not accepted, or the test database is unavailable.');
    sessionStorage.setItem('flowAdminToken', key);
    location.reload();
  } catch (failure) { error.textContent = failure.message; }
};
if (!sessionStorage.getItem('flowAdminToken')) showTestLogin();
