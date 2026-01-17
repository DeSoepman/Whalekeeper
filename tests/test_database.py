import pytest
from datetime import datetime


@pytest.mark.unit
def test_database_init(temp_db):
    """Test database initialization"""
    assert temp_db is not None
    assert temp_db.db_path is not None


@pytest.mark.unit
def test_add_update_history(temp_db):
    """Test adding update history"""
    temp_db.add_update_history(
        container_name="test-container",
        container_id="abc123",
        old_image="test:v1",
        new_image="test:v2",
        old_image_id="img1",
        new_image_id="img2",
        status="success",
        message="Update successful"
    )
    
    history = temp_db.get_update_history(limit=10)
    assert len(history) == 1
    assert history[0]['container_name'] == "test-container"
    assert history[0]['status'] == "success"


@pytest.mark.unit
def test_get_update_history_empty(temp_db):
    """Test getting empty history"""
    history = temp_db.get_update_history()
    assert history == []


@pytest.mark.unit
def test_save_image_version(temp_db):
    """Test saving image version for rollback"""
    config = {
        'image': 'test:v1',
        'name': 'test-container',
        'environment': [],
        'volumes': []
    }
    
    temp_db.save_image_version(
        container_name="test-container",
        image_name="test:v1",
        image_id="img123",
        image_tag="v1",
        container_config=config
    )
    
    versions = temp_db.get_image_versions("test-container")
    assert len(versions) == 1
    assert versions[0]['image_tag'] == "v1"
    assert versions[0]['container_config']['name'] == 'test-container'


@pytest.mark.unit
def test_cleanup_old_versions(temp_db):
    """Test cleanup of old image versions"""
    config = {'image': 'test:v1', 'name': 'test'}
    
    # Add 5 versions
    for i in range(5):
        temp_db.save_image_version(
            container_name="test-container",
            image_name=f"test:v{i}",
            image_id=f"img{i}",
            image_tag=f"v{i}",
            container_config=config
        )
    
    # Keep only 3
    temp_db.cleanup_old_versions("test-container", keep_count=3)
    
    versions = temp_db.get_image_versions("test-container")
    assert len(versions) == 3


@pytest.mark.unit
def test_create_user(temp_db):
    """Test user creation"""
    success = temp_db.create_user("testuser", "hashed_password_123")
    assert success is True
    
    # Try to create duplicate
    success = temp_db.create_user("testuser", "hashed_password_456")
    assert success is False


@pytest.mark.unit
def test_get_user(temp_db):
    """Test getting user"""
    temp_db.create_user("testuser", "hashed_password_123")
    
    user = temp_db.get_user("testuser")
    assert user is not None
    assert user['username'] == "testuser"
    assert user['password_hash'] == "hashed_password_123"
    
    # Non-existent user
    user = temp_db.get_user("nonexistent")
    assert user is None


@pytest.mark.unit
def test_has_users(temp_db):
    """Test checking if users exist"""
    assert temp_db.has_users() is False
    
    temp_db.create_user("testuser", "hashed_password_123")
    assert temp_db.has_users() is True


@pytest.mark.unit
def test_secure_settings(temp_db):
    """Test secure settings storage"""
    temp_db.set_secure_setting("smtp_password", "secret123")
    
    value = temp_db.get_secure_setting("smtp_password")
    assert value == "secret123"
    
    # Non-existent setting
    value = temp_db.get_secure_setting("nonexistent")
    assert value is None
    
    # Update existing
    temp_db.set_secure_setting("smtp_password", "newsecret456")
    value = temp_db.get_secure_setting("smtp_password")
    assert value == "newsecret456"


@pytest.mark.unit
def test_add_check_log(temp_db):
    """Test adding check log when no updates found"""
    temp_db.add_check_log(
        container_name="test-container",
        container_id="abc123",
        current_image="test:v1",
        current_image_id="img123",
        message="No updates available"
    )
    
    history = temp_db.get_update_history(limit=10)
    assert len(history) == 1
    assert history[0]['status'] == "checked"
    assert history[0]['message'] == "No updates available"
