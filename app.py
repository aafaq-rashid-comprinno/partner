import os
import uuid
import yaml
import boto3
import subprocess
import logging
import shutil
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from botocore.exceptions import BotoCoreError, NoCredentialsError

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

def execute_git_command(cmd_list, cwd=None):
    """Execute a Git command inside the repo."""
    try:
        subprocess.run(cmd_list, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd)
    except subprocess.CalledProcessError as e:
        logging.error(f"Git command failed: {e.stderr.decode()}")
        raise

def clone_repository():
    """Clone the GitHub repository if it does not already exist."""
    try:
        github_repo = os.getenv("GITHUB_REPO")
        github_branch = os.getenv("GITHUB_BRANCH", "main")
        repo_name = github_repo.split("/")[-1].replace(".git", "")
        repo_dir = os.path.abspath(repo_name)

        if not os.path.exists(repo_dir):
            logging.info(f"Cloning repository: {github_repo}")
            execute_git_command(["git", "clone", "-b", github_branch, github_repo], cwd=os.getcwd())
        
        return repo_dir
    except Exception as e:
        logging.error(f"Error cloning repository: {e}")
        return None


 # Generates a unique UUID


def generate_ingress_yaml(repo_dir, name):
    """Generate Ingress YAML inside the cloned Git repository."""
    
    try:
        random_uuid = str(uuid.uuid4()) 
        certificate_arn = os.getenv("ACM_CERTIFICATE_ARN")
        load_balancer_name = os.getenv("ALB_NAME")
        ingress_namespace = os.getenv("K8S_NAMESPACE", "tevico")
        ingress_class_name = os.getenv("INGRESS_CLASS_NAME", "alb-common")
        service_port = int(os.getenv("SERVICE_PORT", 80))
        domain_suffix = os.getenv("DOMAIN_SUFFIX", "partner.tevi.co")
        ingress_dir = os.path.join(repo_dir, "ingress")
        os.makedirs(ingress_dir, exist_ok=True)

        file_path = os.path.join(ingress_dir, f"{name}-ingress.yaml")
        
        ingress_template = {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "Ingress",
            "metadata": {
                "labels": {
                    "id": random_uuid,
                    "app": name,
                    "env": os.getenv("ENV", "preprod"),
                    "owner": os.getenv("OWNER", "aafaq"),
                    "component": "ingress",
                    "partner": name
                },
                "annotations": {
                    "alb.ingress.kubernetes.io/certificate-arn": certificate_arn,
                    "alb.ingress.kubernetes.io/load-balancer-name": load_balancer_name,
                    "alb.ingress.kubernetes.io/healthcheck-path": "/",
                    "alb.ingress.kubernetes.io/listen-ports": '[{"HTTP": 80}, {"HTTPS": 443}]',
                    "alb.ingress.kubernetes.io/load-balancer-attributes": "deletion_protection.enabled=false",
                    "alb.ingress.kubernetes.io/ssl-redirect": "443",
                    "alb.ingress.kubernetes.io/target-type": "ip"
                },
                "name": name,
                "namespace": ingress_namespace
            },
            "spec": {
                "ingressClassName": ingress_class_name,
                "rules": [
                    {
                        "host": f"{name}.{domain_suffix}",
                        "http": {
                            "paths": [
                                {
                                    "backend": {
                                        "service": {
                                            "name": "webapp",
                                            "port": {"number": service_port}
                                        }
                                    },
                                    "path": "/",
                                    "pathType": "Prefix"
                                }
                            ]
                        }
                    }
                ]
            }
        }
        
        with open(file_path, "w") as file:
            yaml.dump(ingress_template, file, default_flow_style=False)
        
        logging.info(f"Ingress YAML created: {file_path}")
        return file_path
    except Exception as e:
        logging.error(f"Error generating Ingress YAML: {e}")
        return None

def get_alb_dns():
    """Retrieve ALB DNS name from AWS ALB."""
    try:
        alb_name = os.getenv("ALB_NAME")
        region = os.getenv("AWS_REGION")
        elb_client = boto3.client("elbv2", region_name=region)
        response = elb_client.describe_load_balancers(Names=[alb_name])
        return response["LoadBalancers"][0]["DNSName"]
    except Exception as e:
        logging.error(f"Error retrieving ALB DNS: {e}")
        return None

def update_route53(alb_dns, domain_name):
    """Update Route 53 to map the domain name to the ALB DNS."""
    try:
        hosted_zone_id = os.getenv("ROUTE53_HOSTED_ZONE_ID")
        if not hosted_zone_id:
            raise ValueError("ROUTE53_HOSTED_ZONE_ID environment variable is not set.")

        route53_client = boto3.client("route53")
        response = route53_client.change_resource_record_sets(
            HostedZoneId=hosted_zone_id,
            ChangeBatch={
                "Changes": [
                    {
                        "Action": "UPSERT",
                        "ResourceRecordSet": {
                            "Name": domain_name,
                            "Type": "CNAME",
                            "TTL": 300,
                            "ResourceRecords": [{"Value": alb_dns}]
                        }
                    }
                ]
            }
        )
        logging.info(f"Route 53 updated: {response}")
    except Exception as e:
        logging.error(f"Error updating Route 53: {e}")
        
        
def push_to_github(file_path, repo_dir):
    """Commit, push, merge to main, delete the feature branch, and remove the cloned repo."""
    try:
        github_branch = os.getenv("GITHUB_BRANCH", "main")

        if not os.path.exists(file_path):
            logging.error(f"File {file_path} not found! Aborting Git operation.")
            return

        relative_path = os.path.relpath(file_path, repo_dir)
        timestamp = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
        feature_branch = f"ingress-update-{timestamp}"

        execute_git_command(["git", "checkout", github_branch], cwd=repo_dir)
        execute_git_command(["git", "pull", "--rebase", "origin", github_branch], cwd=repo_dir)
        execute_git_command(["git", "checkout", "-b", feature_branch], cwd=repo_dir)
        
        execute_git_command(["git", "add", relative_path], cwd=repo_dir)
        execute_git_command(["git", "commit", "-m", f"Added ingress YAML: {relative_path}"], cwd=repo_dir)
        execute_git_command(["git", "push", "origin", feature_branch], cwd=repo_dir)

        logging.info(f"Successfully pushed branch: {feature_branch}")

        # Merge feature branch into main
        execute_git_command(["git", "checkout", github_branch], cwd=repo_dir)
        execute_git_command(["git", "merge", feature_branch], cwd=repo_dir)
        execute_git_command(["git", "push", "origin", github_branch], cwd=repo_dir)

        # Delete feature branch
        execute_git_command(["git", "branch", "-d", feature_branch], cwd=repo_dir)
        execute_git_command(["git", "push", "origin", "--delete", feature_branch], cwd=repo_dir)

        logging.info(f"Feature branch {feature_branch} successfully merged and deleted.")

        # Delete cloned repository
        shutil.rmtree(repo_dir)
        logging.info(f"Cloned repository {repo_dir} deleted.")
    except Exception as e:
        logging.error(f"Error pushing to GitHub: {e}")
        
        
@app.route("/deploy", methods=["POST"])
def deploy():
    """Endpoint to trigger the deployment."""
    data = request.get_json()
    partner_name = data.get("partner_name")
    repo_dir = clone_repository()
    if repo_dir:
        yaml_file_path = generate_ingress_yaml(repo_dir, partner_name)
        if yaml_file_path:
            push_to_github(yaml_file_path, repo_dir)
            alb_dns = get_alb_dns()
            if alb_dns:
                domain_name = f"{partner_name}.{os.getenv('DOMAIN_SUFFIX', 'partner.tevi.co')}"
                update_route53(alb_dns, domain_name)
                return jsonify({"message": "Deployment successful", "Domain": domain_name, }), 200
    return jsonify({"message": "Deployment failed"}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
