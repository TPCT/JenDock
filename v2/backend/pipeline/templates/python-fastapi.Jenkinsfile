// Python / FastAPI: install and test with Python, then build the Docker image and deploy it.
// Before the first run, change two things: the service name in "deploy" (as named in your
// compose file) and the address of your health endpoint in "healthCheck".
// Each "sh" step starts a fresh container, so installing and testing share one step.
pipeline {
    agent { docker 'python:3.13-slim' }
    options {
        timeout(time: 20, unit: 'MINUTES')
        disableConcurrentBuilds()
    }
    stages {
        stage('Test') {
            steps {
                sh(label: 'Install and run pytest', script: '''
                    set -e
                    pip install --no-cache-dir -r requirements.txt pytest
                    pytest
                ''')
            }
        }
        stage('Build image') {
            agent none
            steps {
                dockerBuild(image: 'api')
            }
        }
        stage('Deploy') {
            agent none
            steps {
                deploy(service: 'api', image: 'api')
                healthCheck(url: 'https://example.com/health', timeout: 60)
            }
        }
    }
    post {
        failure {
            echo 'The pipeline failed. Open the failed step above to read its log.'
        }
    }
}
