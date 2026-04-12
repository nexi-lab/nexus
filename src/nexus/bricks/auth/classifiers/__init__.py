"""Provider-specific error classifiers for CredentialPool.

Each classifier maps a provider SDK's exceptions to AuthProfileFailureReason.
The pool uses the mapped reason to apply the correct cooldown policy.

Available classifiers:
  - nexus.bricks.auth.classifiers.openai.classify_openai_error
  - nexus.bricks.auth.classifiers.anthropic.classify_anthropic_error
  - nexus.bricks.auth.classifiers.google.classify_google_error
  - nexus.bricks.auth.classifiers.slack.classify_slack_error
  - nexus.bricks.auth.classifiers.boto3.classify_boto3_error
"""
