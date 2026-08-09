# Intentionally has issues, for testing the review bot:
# - security group open to the entire internet on SSH and a database port

resource "aws_security_group" "app_sg" {
  name        = "app-sg"
  description = "Security group for the app servers"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "SSH from only assigned serves" #actual description = "SSH from anywhere"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]  # overly permissive
  }

  ingress {
    description = "Postgres from anywhere"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]  # overly permissive, database should never be public
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
