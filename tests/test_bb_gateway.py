import unittest

from school_mcp.bb.login import gateway_login_url, sso_url_from_html


class GatewayNavigationTests(unittest.TestCase):
    def test_context_sso_after_gateway(self):
        self.assertEqual(sso_url_from_html("https://www.bb.ustc.edu.cn/webapps/login/",
                                          '<a href="https://id.ustc.edu.cn/cas/login?service=synthetic">统一身份认证</a>'),
                         "https://id.ustc.edu.cn/cas/login?service=synthetic")
        for source, target in [("https://evil.example/", "https://id.ustc.edu.cn/cas/login"),
                               ("https://www.bb.ustc.edu.cn/", "https://evil.example/login"),
                               ("https://www.bb.ustc.edu.cn/", "https://id.ustc.edu.cn:8443/cas/login"),
                               ("https://www.bb.ustc.edu.cn/", "https://u@id.ustc.edu.cn/cas/login")]:
            with self.subTest(source=source,target=target):
                self.assertIsNone(sso_url_from_html(source, f'<a href="{target}">统一身份认证</a>'))

    def test_observed_same_origin_gateway(self):
        self.assertEqual(
            gateway_login_url("https://www.bb.ustc.edu.cn/nginx_auth/", '<a href="login.php?next=1234">登录</a>'),
            "https://www.bb.ustc.edu.cn/nginx_auth/login.php?next=1234",
        )

    def test_reject_other_sources(self):
        for source in ["http://www.bb.ustc.edu.cn/nginx_auth/", "https://evil.example/nginx_auth/",
                       "https://www.bb.ustc.edu.cn:8443/nginx_auth/", "https://u@www.bb.ustc.edu.cn/nginx_auth/",
                       "https://www.bb.ustc.edu.cn/webapps/login/"]:
            with self.subTest(source=source):
                self.assertIsNone(gateway_login_url(source, '<a href="login.php">登录</a>'))

    def test_reject_other_destinations_and_ambiguity(self):
        for href in ["https://evil.example/nginx_auth/login.php", "http://www.bb.ustc.edu.cn/nginx_auth/login.php",
                     "https://www.bb.ustc.edu.cn:8443/nginx_auth/login.php", "https://u@www.bb.ustc.edu.cn/nginx_auth/login.php",
                     "/other-action", "login.php#fragment"]:
            with self.subTest(href=href):
                self.assertIsNone(gateway_login_url("https://www.bb.ustc.edu.cn/nginx_auth/", f'<a href="{href}">登录</a>'))
        self.assertIsNone(gateway_login_url("https://www.bb.ustc.edu.cn/nginx_auth/",
                                            '<a href="login.php?next=1">登录</a><a href="login.php?next=2">登录</a>'))


if __name__ == "__main__":
    unittest.main()
