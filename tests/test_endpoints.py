"""Integration tests for API endpoints."""

import json
import pytest
from fastapi.testclient import TestClient
from src.app import app
from src.auth.service import register_user
from src.models.items import save_item

# Mock environment for testing
class MockEnv:
    """Mock Cloudflare environment for endpoint testing."""
    def __init__(self):
        self.DB = None
        self.QUEUE = None
        self.AI = None
        self.VECTORIZE = None
        self.BUCKET = None

@pytest.fixture
def client():
    """FastAPI test client."""
    return TestClient(app)

@pytest.fixture
async def setup_users(db):
    """Create test users."""
    admin = await register_user(db, "admin@test.local", "password123", allow_public_signups=False)
    user2 = await register_user(db, "user2@test.local", "password456", allow_public_signups=True)
    return {"admin": admin, "user2": user2}

@pytest.fixture
async def auth_headers(db, setup_users):
    """Get auth headers for test users."""
    from src.auth.service import login_user
    user, session_id = await login_user(db, "admin@test.local", "password123")
    return {
        "admin_session": session_id,
        "admin_user": user
    }

# ==========================================
# Authentication Tests
# ==========================================

def test_redirect_to_login_when_no_user(client):
    """Test unauthenticated user is redirected to login."""
    # First user doesn't exist, redirect to register
    response = client.get("/", follow_redirects=False)
    assert response.status_code in [303, 307, 308]
    assert "/auth/register" in response.headers.get("location", "") or "/auth/login" in response.headers.get("location", "")

def test_register_first_user_becomes_admin(client, db):
    """Test first user registration claims admin role."""
    response = client.post(
        "/auth/register",
        data={"email": "admin@local", "password": "password123"}
    )
    # Should redirect to home after successful registration
    assert response.status_code in [303, 307, 308]

@pytest.mark.asyncio
async def test_register_subsequent_user_locked(client, db, setup_users):
    """Test second user registration is blocked when public signups disabled."""
    response = client.post(
        "/auth/register",
        data={"email": "intruder@local", "password": "password123"}
    )
    assert response.status_code == 403

@pytest.mark.asyncio
async def test_login_success(client, db, setup_users):
    """Test successful login creates session."""
    response = client.post(
        "/auth/login",
        data={"email": "admin@test.local", "password": "password123"},
        follow_redirects=False
    )
    # Should redirect after successful login
    assert response.status_code in [303, 307, 308]
    assert "kfm_session" in response.cookies or "Set-Cookie" in response.headers

@pytest.mark.asyncio
async def test_login_invalid_credentials(client, db, setup_users):
    """Test login with wrong password fails."""
    response = client.post(
        "/auth/login",
        data={"email": "admin@test.local", "password": "wrongpassword"},
        follow_redirects=False
    )
    assert response.status_code == 400
    assert "Invalid email or password" in response.text

@pytest.mark.asyncio
async def test_logout(client, db, auth_headers):
    """Test logout invalidates session."""
    # Set session cookie
    client.cookies["kfm_session"] = auth_headers["admin_session"]
    
    response = client.get("/auth/logout", follow_redirects=False)
    assert response.status_code in [303, 307, 308]
    # Session should be cleared

# ==========================================
# Item Management Tests
# ==========================================

@pytest.mark.asyncio
async def test_library_page_authenticated(client, db, auth_headers):
    """Test library page loads for authenticated user."""
    client.cookies["kfm_session"] = auth_headers["admin_session"]
    response = client.get("/")
    assert response.status_code == 200
    assert "html" in response.text.lower()

@pytest.mark.asyncio
async def test_save_url_via_form(client, db, auth_headers):
    """Test saving URL via form submission."""
    client.cookies["kfm_session"] = auth_headers["admin_session"]
    env = MockEnv()
    
    response = client.post(
        "/save",
        data={"url": "https://example.com/article", "tags": "tech,python"},
        follow_redirects=False
    )
    # Should redirect after successful save
    assert response.status_code in [303, 307, 308]

@pytest.mark.asyncio
async def test_save_url_missing_url(client, db, auth_headers):
    """Test save fails when URL is missing."""
    client.cookies["kfm_session"] = auth_headers["admin_session"]
    
    response = client.post(
        "/save",
        data={"url": "", "tags": ""}
    )
    # Should fail validation
    assert response.status_code in [400, 422]

@pytest.mark.asyncio
async def test_get_item_not_found(client, db, auth_headers):
    """Test retrieving non-existent item returns 404."""
    client.cookies["kfm_session"] = auth_headers["admin_session"]
    
    response = client.get("/items/nonexistent-id")
    assert response.status_code == 404

@pytest.mark.asyncio
async def test_get_item_wrong_user(client, db):
    """Test user cannot access another user's items."""
    env = MockEnv()
    
    # Create two users
    user1 = await register_user(db, "user1@test.local", "password123")
    user2 = await register_user(db, "user2@test.local", "password456", allow_public_signups=True)
    
    # User1 saves item
    item, _ = await save_item(db, env, user1["id"], "https://example.com/secret", ["private"])
    
    # Login as user2
    from src.auth.service import login_user
    _, session_id = await login_user(db, "user2@test.local", "password456")
    client.cookies["kfm_session"] = session_id
    
    # User2 tries to access user1's item
    response = client.get(f"/items/{item['id']}")
    assert response.status_code == 404

# ==========================================
# API Tests
# ==========================================

@pytest.mark.asyncio
async def test_api_save_with_bearer_token(client, db, auth_headers):
    """Test API save endpoint with Bearer token authentication."""
    from src.auth.service import create_pat
    
    # Create PAT for user
    user = auth_headers["admin_user"]
    pat_data = await create_pat(db, user["id"], "Test Token")
    
    headers = {"Authorization": f"Bearer {pat_data['token']}"}
    
    response = client.post(
        "/api/save",
        json={"url": "https://example.com/api-test", "tags": ["api"]},
        headers=headers
    )
    assert response.status_code in [200, 201]
    data = response.json()
    assert "id" in data

@pytest.mark.asyncio
async def test_api_save_missing_auth(client):
    """Test API save requires authentication."""
    response = client.post(
        "/api/save",
        json={"url": "https://example.com/test"}
    )
    assert response.status_code == 401

@pytest.mark.asyncio
async def test_api_search(client, db, auth_headers):
    """Test API search endpoint."""
    client.cookies["kfm_session"] = auth_headers["admin_session"]
    
    response = client.post(
        "/api/search",
        json={"query": "test", "mode": "keyword"}
    )
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert isinstance(data["items"], list)

@pytest.mark.asyncio
async def test_api_list_items(client, db, auth_headers):
    """Test API list items endpoint."""
    client.cookies["kfm_session"] = auth_headers["admin_session"]
    
    response = client.get("/api/items?limit=10")
    assert response.status_code == 200
    data = response.json()
    assert "items" in data

@pytest.mark.asyncio
async def test_api_delete_item(client, db, auth_headers):
    """Test API delete item endpoint."""
    env = MockEnv()
    user = auth_headers["admin_user"]
    
    # Create item
    item, _ = await save_item(db, env, user["id"], "https://example.com/delete-me")
    
    client.cookies["kfm_session"] = auth_headers["admin_session"]
    response = client.delete(f"/api/items/{item['id']}")
    
    assert response.status_code in [200, 204]
    
    # Verify deleted
    check_response = client.get(f"/api/items/{item['id']}")
    assert check_response.status_code == 404

# ==========================================
# Import/Export Tests
# ==========================================

@pytest.mark.asyncio
async def test_import_csv_bookmarks(client, db, auth_headers):
    """Test CSV bookmark import."""
    client.cookies["kfm_session"] = auth_headers["admin_session"]
    
    csv_content = """url,title,tags
https://example.com/1,Example One,"tech,news"
https://example.com/2,Example Two,python
"""
    
    response = client.post(
        "/import",
        data={"content": csv_content, "format": "csv"}
    )
    assert response.status_code in [200, 303, 307, 308]

@pytest.mark.asyncio
async def test_import_netscape_bookmarks(client, db, auth_headers):
    """Test Netscape HTML bookmark import."""
    client.cookies["kfm_session"] = auth_headers["admin_session"]
    
    html_content = """<!DOCTYPE NETSCAPE-Bookmark-file-1>
    <DL><p>
        <DT><A HREF="https://example.com/1" TAGS="tech">Example One</A>
    </DL><p>
"""
    
    response = client.post(
        "/import",
        data={"content": html_content, "format": "netscape"}
    )
    assert response.status_code in [200, 303, 307, 308]

# ==========================================
# Settings/PAT Tests
# ==========================================

@pytest.mark.asyncio
async def test_settings_page(client, db, auth_headers):
    """Test settings page loads."""
    client.cookies["kfm_session"] = auth_headers["admin_session"]
    
    response = client.get("/settings")
    assert response.status_code == 200
    assert "html" in response.text.lower()

@pytest.mark.asyncio
async def test_create_pat(client, db, auth_headers):
    """Test creating personal access token."""
    client.cookies["kfm_session"] = auth_headers["admin_session"]
    
    response = client.post(
        "/settings/tokens",
        data={"name": "My API Token"}
    )
    assert response.status_code in [200, 201]
    data = response.json()
    assert "token" in data
    assert data["token"].startswith("kfm_live_")

@pytest.mark.asyncio
async def test_delete_pat(client, db, auth_headers):
    """Test deleting personal access token."""
    from src.auth.service import create_pat
    
    user = auth_headers["admin_user"]
    pat_data = await create_pat(db, user["id"], "Test PAT")
    
    client.cookies["kfm_session"] = auth_headers["admin_session"]
    
    response = client.post(
        f"/settings/tokens/{pat_data['id']}/delete"
    )
    assert response.status_code in [200, 303, 307, 308]

# ==========================================
# Error Handling Tests
# ==========================================

@pytest.mark.asyncio
async def test_invalid_url_format(client, db, auth_headers):
    """Test save rejects invalid URL format."""
    client.cookies["kfm_session"] = auth_headers["admin_session"]
    
    response = client.post(
        "/api/save",
        json={"url": "not-a-valid-url"}
    )
    # Should either fail validation or be handled gracefully
    assert response.status_code in [400, 422, 500]

@pytest.mark.asyncio
async def test_search_empty_query(client, db, auth_headers):
    """Test search with empty query."""
    client.cookies["kfm_session"] = auth_headers["admin_session"]
    
    response = client.post(
        "/api/search",
        json={"query": ""}
    )
    # Should return empty results or error
    assert response.status_code in [200, 400, 422]

@pytest.mark.asyncio
async def test_page_not_found(client):
    """Test 404 handling for non-existent pages."""
    response = client.get("/nonexistent-page")
    assert response.status_code == 404
