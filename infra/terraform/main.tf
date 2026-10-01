# GitHub Actions OIDC access for sre-toolkit.
#
# Cost: $0. An IAM role, an OIDC provider and a policy are all free resources.
# There is no agent, no collector, no always-on compute anywhere in this project —
# the toolkit runs inside the job that needs it and exits.
#
# Security: no long-lived access keys exist. GitHub exchanges a short-lived OIDC
# token for a session on this role, scoped to one repository and (by default) to
# the branches you name.

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

variable "region" {
  description = "Region the toolkit reads from."
  type        = string
  default     = "us-east-1"
}

variable "github_repository" {
  description = "owner/repo allowed to assume the role."
  type        = string
  default     = "Jenom/sre-toolkit"
}

variable "allowed_refs" {
  description = "Git refs allowed to assume the role."
  type        = list(string)
  default     = ["refs/heads/main"]
}

variable "create_oidc_provider" {
  description = "Set false if the GitHub OIDC provider already exists in this account."
  type        = bool
  default     = true
}

variable "enable_bedrock" {
  description = "Attach the Bedrock invoke policy. Leave false to make AI analysis impossible to bill."
  type        = bool
  default     = false
}

variable "bedrock_model_id" {
  description = "The single model the role may invoke when enable_bedrock is true."
  type        = string
  default     = "anthropic.claude-haiku-4-5"
}

data "aws_caller_identity" "current" {}

resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 1 : 0

  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

locals {
  oidc_arn = var.create_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : "arn:aws:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/token.actions.githubusercontent.com"
  subjects = [for ref in var.allowed_refs : "repo:${var.github_repository}:ref:${ref}"]
}

data "aws_iam_policy_document" "trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.oidc_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = local.subjects
    }
  }
}

resource "aws_iam_role" "sre_toolkit" {
  name                 = "sre-toolkit-readonly"
  description          = "Read-only incident investigation access for sre-toolkit."
  assume_role_policy   = data.aws_iam_policy_document.trust.json
  max_session_duration = 3600
}

resource "aws_iam_policy" "readonly" {
  name   = "sre-toolkit-readonly"
  policy = file("${path.module}/../iam/sre-toolkit-readonly.json")
}

resource "aws_iam_role_policy_attachment" "readonly" {
  role       = aws_iam_role.sre_toolkit.name
  policy_arn = aws_iam_policy.readonly.arn
}

# Region guard: the role cannot be used to read (and therefore bill) outside the
# region this toolkit is deployed to look at.
resource "aws_iam_role_policy" "region_boundary" {
  name = "region-boundary"
  role = aws_iam_role.sre_toolkit.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "DenyOtherRegions"
        Effect   = "Deny"
        Action   = ["cloudwatch:*", "logs:*", "ecs:*", "rds:*", "elasticloadbalancing:*", "ec2:*"]
        Resource = "*"
        Condition = {
          StringNotEquals = { "aws:RequestedRegion" = [var.region] }
        }
      }
    ]
  })
}

resource "aws_iam_policy" "bedrock" {
  count = var.enable_bedrock ? 1 : 0

  name = "sre-toolkit-bedrock-invoke"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "InvokeCheapestModelOnly"
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = ["arn:aws:bedrock:*::foundation-model/${var.bedrock_model_id}"]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "bedrock" {
  count = var.enable_bedrock ? 1 : 0

  role       = aws_iam_role.sre_toolkit.name
  policy_arn = aws_iam_policy.bedrock[0].arn
}

output "role_arn" {
  description = "Set this as AWS_ROLE_ARN in the GitHub repository."
  value       = aws_iam_role.sre_toolkit.arn
}

output "monthly_cost_estimate_usd" {
  description = "Standing infrastructure cost of this toolkit."
  value       = "0.00 — IAM roles, policies and OIDC providers are free; the CLI runs on demand."
}
