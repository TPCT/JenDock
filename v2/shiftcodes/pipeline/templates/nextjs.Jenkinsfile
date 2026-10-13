// Next.js: install, check and build with Node, then build the Docker image and deploy it.
// Before the first run, change two things: the service name in "deploy" (as named in your
// compose file) and the address in "healthCheck".
pipeline {
    agent { docker 'node:22' }
    options {
        timeout(time: 30, unit: 'MINUTES')
        disableConcurrentBuilds()
    }
    stages {
        stage('Install') {
            steps {
                sh 'npm ci'
            }
        }
        stage('Check') {
            parallel {
                stage('Lint') {
                    steps {
                        sh 'npm run lint'
                    }
                }
                stage('Test') {
                    steps {
                        sh 'npm test --if-present'
                    }
                }
            }
        }
        stage('Build') {
            steps {
                sh 'npm run build'
            }
        }
        stage('Build image') {
            agent none
            steps {
                dockerBuild(image: 'web')
            }
        }
        stage('Deploy') {
            agent none
            steps {
                deploy(service: 'web', image: 'web')
                healthCheck(url: 'https://example.com', timeout: 90)
            }
        }
    }
    post {
        failure {
            echo 'The pipeline failed. Open the failed step above to read its log.'
        }
    }
}
