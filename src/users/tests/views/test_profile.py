from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


class Profile(TestCase):
    """The Hearth fork delegates account management to its identity provider."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="test")
        self.client.force_login(self.user)

    def test_account_page_redirects_without_local_account_controls(self):
        response = self.client.get(reverse("account"), follow=True)
        self.assertRedirects(response, reverse("preferences"))
        self.assertNotContains(response, 'href="/settings/account"')
        self.assertNotContains(response, 'name="old_password"')

    def test_account_post_cannot_change_identity_or_password(self):
        original_password = self.user.password
        for payload in (
            {"username": "new_test", "email": "new@example.com"},
            {"new_password1": "changed-password", "new_password2": "changed-password"},
        ):
            response = self.client.post(reverse("account"), payload)
            self.assertRedirects(response, reverse("preferences"))
            self.user.refresh_from_db()
            self.assertEqual(self.user.username, "test")
            self.assertEqual(self.user.email, "")
            self.assertEqual(self.user.password, original_password)
