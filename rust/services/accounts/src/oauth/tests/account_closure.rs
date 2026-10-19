use super::*;

fn authenticated<T>(token: &str, body: T) -> Request<T> {
    let mut request = Request::new(body);
    request
        .metadata_mut()
        .insert("authorization", format!("Bearer {token}").parse().unwrap());
    request
}

#[tokio::test]
async fn deleting_a_google_account_is_atomic_across_replicas_and_preserves_other_identities() {
    for postgres in databases() {
        for through_http in [true, false] {
            let database = DatabaseFixture::new(postgres).await;
            database.execute("INSERT INTO users (user_id,email,primary_auth_method,is_global_admin,is_active,email_verified,created_at,updated_at) VALUES ('unrelated','unrelated@example.test','oauth',0,1,1,'2000-01-01 00:00:00','2000-01-01 00:00:00'); INSERT INTO user_oauth_accounts (oauth_account_id,user_id,provider,provider_user_id,created_at) VALUES ('unrelated-binding','unrelated','google','unrelated-subject','2000-01-01 00:00:00');").await;
            let provider = ProviderFixture::new().await;
            let first = Running::new(&database, &provider).await;
            let second = Running::new(&database, &provider).await;
            let (start, cookie) = first.begin().await;
            let (code, state) = provider
                .code(start["auth_url"].as_str().unwrap(), json!({}), 1, 1)
                .await;
            let (status, login) = first.callback(&code, &state, &cookie).await;
            assert_eq!(status, 200);
            let token = login["token"].as_str().unwrap();
            let old_id = login["user"]["user_id"].as_str().unwrap();
            uuid::Uuid::parse_str(old_id).unwrap();
            let http = reqwest::Client::new();
            let endpoint = format!("{}/auth/me", first.http_url);
            let other_endpoint = format!("{}/auth/me", second.http_url);
            let mut client = second.client().await;

            // A cascade failure must roll back the parent identity as well.
            database.execute(if postgres {
                "CREATE FUNCTION block_binding_delete() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'fixture binding delete failure'; END; $$; CREATE TRIGGER block_binding_delete BEFORE DELETE ON user_oauth_accounts FOR EACH ROW EXECUTE FUNCTION block_binding_delete();"
            } else {
                "CREATE TRIGGER block_binding_delete BEFORE DELETE ON user_oauth_accounts BEGIN SELECT RAISE(ABORT, 'fixture binding delete failure'); END;"
            }).await;
            if through_http {
                assert_eq!(
                    http.delete(&endpoint)
                        .bearer_auth(token)
                        .send()
                        .await
                        .unwrap()
                        .status(),
                    503
                );
            } else {
                assert_eq!(
                    client
                        .delete_account(authenticated(token, DeleteAccountRequest {}))
                        .await
                        .unwrap_err()
                        .code(),
                    tonic::Code::Unavailable
                );
            }
            assert_eq!(database.count("SELECT count(*) FROM users").await, 2);
            assert_eq!(
                database
                    .count("SELECT count(*) FROM user_oauth_accounts")
                    .await,
                2
            );
            client
                .get_profile(authenticated(token, GetProfileRequest {}))
                .await
                .unwrap();
            database.execute(if postgres {
                "DROP TRIGGER block_binding_delete ON user_oauth_accounts; DROP FUNCTION block_binding_delete();"
            } else { "DROP TRIGGER block_binding_delete;" }).await;

            if through_http {
                let response = http
                    .delete(&endpoint)
                    .bearer_auth(token)
                    .send()
                    .await
                    .unwrap();
                assert_eq!(response.status(), 200);
                assert_eq!(
                    response.json::<Value>().await.unwrap()["message"],
                    "Account deleted"
                );
            } else {
                assert_eq!(
                    client
                        .delete_account(authenticated(token, DeleteAccountRequest {}))
                        .await
                        .unwrap()
                        .into_inner()
                        .message,
                    "Account deleted"
                );
            }
            assert_eq!(
                http.get(&other_endpoint)
                    .bearer_auth(token)
                    .send()
                    .await
                    .unwrap()
                    .status(),
                401
            );
            assert_eq!(
                client
                    .get_profile(authenticated(token, GetProfileRequest {}))
                    .await
                    .unwrap_err()
                    .code(),
                tonic::Code::Unauthenticated
            );
            assert_eq!(
                database
                    .count("SELECT count(*) FROM users WHERE user_id = 'unrelated'")
                    .await,
                1
            );
            assert_eq!(database.count("SELECT count(*) FROM users").await, 1);
            assert_eq!(
                database
                    .count("SELECT count(*) FROM user_oauth_accounts WHERE user_id = 'unrelated'")
                    .await,
                1
            );
            assert_eq!(
                database
                    .count("SELECT count(*) FROM user_oauth_accounts")
                    .await,
                1
            );
            first.close().await;
            second.close().await;

            let restarted = Running::new(&database, &provider).await;
            assert_eq!(
                restarted
                    .client()
                    .await
                    .get_profile(authenticated(token, GetProfileRequest {}))
                    .await
                    .unwrap_err()
                    .code(),
                tonic::Code::Unauthenticated
            );
            // A later sign-up creates a new identity without restoring the old
            // account ID or copying any native zone or permission facts.
            let (start, cookie) = restarted.begin().await;
            let (code, state) = provider
                .code(start["auth_url"].as_str().unwrap(), json!({}), 1, 1)
                .await;
            let (status, fresh) = restarted.callback(&code, &state, &cookie).await;
            assert_eq!(status, 200);
            assert_ne!(fresh["user"]["user_id"], old_id);
            assert_eq!(fresh["user"]["is_global_admin"], false);
            assert!(fresh["user"]["zone_id"].is_null());
            assert!(fresh.get("api_key").is_none());
            assert_eq!(
                restarted
                    .client()
                    .await
                    .get_profile(authenticated(token, GetProfileRequest {}))
                    .await
                    .unwrap_err()
                    .code(),
                tonic::Code::Unauthenticated
            );
            restarted.close().await;
            database.clean().await;
        }
    }
}
