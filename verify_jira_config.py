"""
JIRA Configuration Verification Script
======================================
Run this to test your JIRA connection and configuration.

Usage:
    python verify_jira_config.py
    python verify_jira_config.py --create-test-ticket
"""
import sys
from pathlib import Path

# Add src to path
_ROOT = Path(__file__).parent
sys.path.insert(0, str(_ROOT / "src"))

from dotenv import load_dotenv
load_dotenv()

from src.gemini_connector import jira_client


def verify_config():
    """Verify JIRA configuration."""
    print("🔍 JIRA Configuration Verification")
    print("=" * 60)
    
    # Check if configured
    if not jira_client.is_configured():
        print("❌ JIRA is NOT configured")
        print("\nMissing environment variables:")
        
        missing = []
        if not jira_client.JIRA_URL:
            missing.append("  - JIRA_URL")
        if not jira_client.JIRA_EMAIL:
            missing.append("  - JIRA_EMAIL")
        if not jira_client.JIRA_API_TOKEN:
            missing.append("  - JIRA_API_TOKEN")
        if not jira_client.JIRA_PROJECT_KEY:
            missing.append("  - JIRA_PROJECT_KEY")
        
        print("\n".join(missing))
        print("\n📝 Add these variables to your .env file:")
        print("""
JIRA_URL=https://your-company.atlassian.net
JIRA_EMAIL=your.email@company.com
JIRA_API_TOKEN=your_api_token_here
JIRA_PROJECT_KEY=MIG
        """)
        return False
    
    print("✅ JIRA is configured")
    print(f"\n📋 Configuration Details:")
    print(f"  - URL: {jira_client.JIRA_URL}")
    print(f"  - Email: {jira_client.JIRA_EMAIL}")
    print(f"  - API Token: {'*' * 20} (hidden)")
    print(f"  - Project Key: {jira_client.JIRA_PROJECT_KEY}")
    print(f"  - Issue Type: {jira_client.JIRA_ISSUE_TYPE}")
    
    return True


def test_connection():
    """Test connection to JIRA."""
    print("\n🔗 Testing JIRA Connection")
    print("=" * 60)
    
    try:
        import requests
        resp = requests.get(
            f"{jira_client.JIRA_URL}/rest/api/3/myself",
            auth=(jira_client.JIRA_EMAIL, jira_client.JIRA_API_TOKEN),
            timeout=10
        )
        
        if resp.status_code == 200:
            user_data = resp.json()
            print(f"✅ Successfully connected to JIRA")
            print(f"  - Account: {user_data.get('displayName', 'Unknown')}")
            print(f"  - Email: {user_data.get('emailAddress', 'Unknown')}")
            return True
        else:
            print(f"❌ Connection failed: {resp.status_code}")
            print(f"   Response: {resp.text[:200]}")
            return False
            
    except Exception as e:
        print(f"❌ Connection error: {e}")
        return False


def test_project_access():
    """Test access to the configured project."""
    print("\n📁 Testing Project Access")
    print("=" * 60)
    
    try:
        import requests
        resp = requests.get(
            f"{jira_client.JIRA_URL}/rest/api/3/project/{jira_client.JIRA_PROJECT_KEY}",
            auth=(jira_client.JIRA_EMAIL, jira_client.JIRA_API_TOKEN),
            timeout=10
        )
        
        if resp.status_code == 200:
            project_data = resp.json()
            print(f"✅ Project access confirmed")
            print(f"  - Project: {project_data.get('name', 'Unknown')}")
            print(f"  - Key: {project_data.get('key', 'Unknown')}")
            print(f"  - Type: {project_data.get('projectTypeKey', 'Unknown')}")
            return True
        elif resp.status_code == 404:
            print(f"❌ Project '{jira_client.JIRA_PROJECT_KEY}' not found")
            print(f"   Please verify the project key in your .env file")
            return False
        else:
            print(f"❌ Access denied: {resp.status_code}")
            print(f"   Response: {resp.text[:200]}")
            return False
            
    except Exception as e:
        print(f"❌ Access test error: {e}")
        return False


def create_test_ticket():
    """Create a test JIRA ticket."""
    print("\n🎫 Creating Test Ticket")
    print("=" * 60)
    
    try:
        result = jira_client.create_ticket(
            summary="[TEST] Migration Validator - Configuration Test",
            description=(
                "This is a test ticket created by the Migration Validator "
                "to verify JIRA integration is working correctly.\n\n"
                "✅ If you see this ticket, your JIRA integration is properly configured!\n\n"
                "You can safely delete this ticket."
            ),
            labels=["test", "migration-validator", "auto-generated"]
        )
        
        print(f"✅ Test ticket created successfully!")
        print(f"  - Key: {result['key']}")
        print(f"  - URL: {result['url']}")
        print(f"\n🌐 Open in browser: {result['url']}")
        return True
        
    except jira_client.JiraError as e:
        print(f"❌ Failed to create ticket: {e}")
        return False
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return False


def main():
    """Main verification flow."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Verify JIRA configuration")
    parser.add_argument(
        "--create-test-ticket",
        action="store_true",
        help="Create a test ticket in JIRA"
    )
    args = parser.parse_args()
    
    print("\n🚀 Migration Validator - JIRA Verification")
    print("=" * 60)
    
    # Step 1: Check configuration
    if not verify_config():
        sys.exit(1)
    
    # Step 2: Test connection
    if not test_connection():
        sys.exit(1)
    
    # Step 3: Test project access
    if not test_project_access():
        sys.exit(1)
    
    # Step 4: Optionally create test ticket
    if args.create_test_ticket:
        if not create_test_ticket():
            sys.exit(1)
    else:
        print("\n💡 Tip: Run with --create-test-ticket to create a test ticket")
    
    print("\n" + "=" * 60)
    print("✅ All checks passed! JIRA integration is ready to use.")
    print("=" * 60)


if __name__ == "__main__":
    main()
