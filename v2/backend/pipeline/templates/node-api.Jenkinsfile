// Node.js API: install and test with Node, then build the Docker image and deploy it.
// Before the first run, change two things: the service name in "deploy" (as named in your
// compose file) and the address of your health endpoint in "healthCheck".
pipeline {
    agent { docker 'node:22' }
    options {
        timeout(time: 20, unit: 'MINUTES')
        disableConcurrentBuilds()
    }
    stages {
        stage('Install') {
            steps {
                sh 'npm ci'
            }
        }
        stage('Test') {
            steps {
                sh 'npm test'
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
