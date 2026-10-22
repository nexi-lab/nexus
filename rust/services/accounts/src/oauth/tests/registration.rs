use super::*;

#[tokio::test]
async fn registration_recovers_from_repeated_serialization_conflicts_without_partial_users() {
    if !databases().contains(&true) {
        return;
    }
    for google in [false, true] {
        let database = DatabaseFixture::new(true).await;
        database
            .execute(
                "CREATE SEQUENCE registration_attempt;
             CREATE FUNCTION conflict_registration() RETURNS trigger LANGUAGE plpgsql AS $$
             BEGIN
               IF nextval('registration_attempt') <= 4 THEN
                 RAISE EXCEPTION 'fixture serialization conflict' USING ERRCODE = '40001';
               END IF;
               RETURN NEW;
             END $$;
             CREATE TRIGGER conflict_registration BEFORE INSERT ON users
             FOR EACH ROW EXECUTE FUNCTION conflict_registration();",
            )
            .await;
        let provider = ProviderFixture::new().await;
        let running = Running::new(&database, &provider).await;
        let user_id = if google {
            let (start, cookie) = running.begin().await;
            let (code, state) = provider
                .code(
                    start["auth_url"].as_str().unwrap(),
                    json!({"sub":"registration-retry", "email":"retry@example.test"}),
                    1,
                    1,
                )
                .await;
            let (status, response) = running.callback(&code, &state, &cookie).await;
            assert_eq!(status, 200, "{response}");
            assert_eq!(
                provider.token_requests(),
                1,
                "Provider code must only be exchanged once"
            );
            response["user"]["user_id"].as_str().unwrap().to_owned()
        } else {
            running
                .client()
                .await
                .register(RegisterRequest {
                    email: "retry@example.test".into(),
                    password: "registration-retry-password".into(),
                    username: None,
                    display_name: None,
                })
                .await
                .unwrap()
                .into_inner()
                .user_id
        };
        assert!(!user_id.is_empty());
        // PostgreSQL sequences survive rollback, so this verifies real failed
        // transactions rather than depending on thread timing or a sleep.
        assert_eq!(
            database
                .count("SELECT last_value FROM registration_attempt")
                .await,
            5
        );
        assert_eq!(database.count("SELECT count(*) FROM users").await, 1);
        assert_eq!(
            database
                .count("SELECT count(*) FROM user_oauth_accounts")
                .await,
            i64::from(google)
        );
        running.close().await;
        database.clean().await;
    }
}
