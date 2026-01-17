import sqlite3
import json
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional


class Database:
    def __init__(self, db_path: str = "data/updater.db"):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.init_db()
    
    def init_db(self):
        """Initialize database schema"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Update history table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS update_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                container_name TEXT NOT NULL,
                container_id TEXT NOT NULL,
                old_image TEXT NOT NULL,
                new_image TEXT NOT NULL,
                old_image_id TEXT NOT NULL,
                new_image_id TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT,
                timestamp TEXT NOT NULL,
                health_check_passed INTEGER DEFAULT NULL,
                rollback_reason TEXT DEFAULT NULL
            )
        """)
        
        # Add columns to existing table if they don't exist
        try:
            cursor.execute("ALTER TABLE update_history ADD COLUMN health_check_passed INTEGER DEFAULT NULL")
        except sqlite3.OperationalError:
            pass  # Column already exists
        
        try:
            cursor.execute("ALTER TABLE update_history ADD COLUMN rollback_reason TEXT DEFAULT NULL")
        except sqlite3.OperationalError:
            pass  # Column already exists
        
        # Image versions table for rollback
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS image_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                container_name TEXT NOT NULL,
                image_name TEXT NOT NULL,
                image_id TEXT NOT NULL,
                image_tag TEXT NOT NULL,
                container_config TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        
        # Users table for authentication
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        
        # Secure settings table for encrypted credentials
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS secure_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        
        conn.commit()
        conn.close()
    
    def add_update_history(self, container_name: str, container_id: str, 
                          old_image: str, new_image: str,
                          old_image_id: str, new_image_id: str,
                          status: str, message: str = "",
                          health_check_passed: Optional[bool] = None,
                          rollback_reason: Optional[str] = None):
        """Record an update attempt"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        health_check_int = None if health_check_passed is None else (1 if health_check_passed else 0)
        
        cursor.execute("""
            INSERT INTO update_history 
            (container_name, container_id, old_image, new_image, 
             old_image_id, new_image_id, status, message, timestamp,
             health_check_passed, rollback_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (container_name, container_id, old_image, new_image,
              old_image_id, new_image_id, status, message, 
              datetime.now().isoformat(), health_check_int, rollback_reason))
        
        conn.commit()
        conn.close()
    
    def add_check_log(self, container_name: str, container_id: str,
                     current_image: str, current_image_id: str,
                     message: str = "No updates available"):
        """Record a check event when no updates are found"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO update_history 
            (container_name, container_id, old_image, new_image, 
             old_image_id, new_image_id, status, message, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (container_name, container_id, current_image, current_image,
              current_image_id, current_image_id, "checked", message, 
              datetime.now().isoformat()))
        
        conn.commit()
        conn.close()
    
    def save_image_version(self, container_name: str, image_name: str,
                          image_id: str, image_tag: str, 
                          container_config: Dict):
        """Save image version for rollback"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO image_versions 
            (container_name, image_name, image_id, image_tag, 
             container_config, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (container_name, image_name, image_id, image_tag,
              json.dumps(container_config), datetime.now().isoformat()))
        
        conn.commit()
        conn.close()
    
    def get_update_history(self, limit: int = 50) -> List[Dict]:
        """Get recent update history"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT * FROM update_history 
            ORDER BY timestamp DESC 
            LIMIT ?
        """, (limit,))
        
        rows = cursor.fetchall()
        conn.close()
        
        return [dict(row) for row in rows]
    
    def get_image_versions(self, container_name: str) -> List[Dict]:
        """Get available image versions for a container"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT * FROM image_versions 
            WHERE container_name = ?
            ORDER BY created_at DESC
        """, (container_name,))
        
        rows = cursor.fetchall()
        conn.close()
        
        result = []
        for row in rows:
            data = dict(row)
            data['container_config'] = json.loads(data['container_config'])
            result.append(data)
        
        return result
    
    def cleanup_old_versions(self, container_name: str, keep_count: int):
        """Remove old image versions, keeping only the most recent ones"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            DELETE FROM image_versions 
            WHERE container_name = ? 
            AND id NOT IN (
                SELECT id FROM image_versions 
                WHERE container_name = ?
                ORDER BY created_at DESC 
                LIMIT ?
            )
        """, (container_name, container_name, keep_count))
        
        conn.commit()
        conn.close()
    
    def create_user(self, username: str, password_hash: str) -> bool:
        """Create a new user"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT INTO users (username, password_hash, created_at)
                VALUES (?, ?, ?)
            """, (username, password_hash, datetime.now().isoformat()))
            
            conn.commit()
            conn.close()
            return True
        except sqlite3.IntegrityError:
            return False
    
    def get_user(self, username: str) -> Optional[Dict]:
        """Get user by username"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT id, username, password_hash, created_at
            FROM users
            WHERE username = ?
        """, (username,))
        
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return dict(row)
        return None
    
    def has_users(self) -> bool:
        """Check if any users exist"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) FROM users")
        count = cursor.fetchone()[0]
        
        conn.close()
        return count > 0
    
    def set_secure_setting(self, key: str, value: str):
        """Store a secure setting (like SMTP password)"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT OR REPLACE INTO secure_settings (key, value, updated_at)
            VALUES (?, ?, ?)
        """, (key, value, datetime.now().isoformat()))
        
        conn.commit()
        conn.close()
    
    def get_secure_setting(self, key: str) -> Optional[str]:
        """Retrieve a secure setting"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT value FROM secure_settings WHERE key = ?
        """, (key,))
        
        row = cursor.fetchone()
        conn.close()
        
        return row[0] if row else None
