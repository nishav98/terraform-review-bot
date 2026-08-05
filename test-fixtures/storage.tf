# Intentionally has issues, for testing the review bot:
# - three near-identical S3 buckets copy-pasted instead of using for_each
# - inconsistent naming convention (mix of snake_case and kebab-case, no environment prefix)
# - no versioning or encryption configured

resource "aws_s3_bucket" "logsBucket" {
  bucket = "myapp-logs"
}

resource "aws_s3_bucket" "uploads-bucket" {
  bucket = "myapp-uploads"
}

resource "aws_s3_bucket" "backup_bucket_2" {
  bucket = "myapp-backups"
}
