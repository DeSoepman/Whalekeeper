import pytest
from passlib.context import CryptContext

from app.web.routes import pwd_context


@pytest.mark.unit
def test_password_hashing():
    """Test password hashing and verification"""
    password = "testpassword123"
    
    # Hash password
    hashed = pwd_context.hash(password)
    
    # Verify correct password
    assert pwd_context.verify(password, hashed) is True
    
    # Verify incorrect password
    assert pwd_context.verify("wrongpassword", hashed) is False


@pytest.mark.unit
def test_different_passwords_different_hashes():
    """Test that same password generates different hashes (salt)"""
    password = "testpassword123"
    
    hash1 = pwd_context.hash(password)
    hash2 = pwd_context.hash(password)
    
    # Hashes should be different (due to salt)
    assert hash1 != hash2
    
    # But both should verify
    assert pwd_context.verify(password, hash1) is True
    assert pwd_context.verify(password, hash2) is True
