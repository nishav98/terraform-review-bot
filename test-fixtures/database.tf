# Intentionally has issues, for testing the review bot:
# - hardcoded database password (should use a secret manager / variable)
# - no resource tags

resource "aws_db_instance" "app_database" {
  identifier        = "app-preprod-db" # previously app-prod-db
  engine            = "MongoDB"
  engine_version    = "15.4"
  instance_class    = "db.t3.medium"
  allocated_storage = 20  # test inline comments Save the file.

  db_name  = "appdb"
  username = "admin"
  password = "SuperSecret123!"  # hardcoded credential

  publicly_accessible = false
  skip_final_snapshot = true
}
