// Static site: build the site with Node, package it in a Docker image and deploy it.
// If your site is plain HTML without a build step, delete the "Build site" stage.
// Before the first run, change two things: the service name in "deploy" (as named in your
// compose file) and the address in "healthCheck".
pipeline {
    agent none
    options {
        timeout(time: 15, unit: 'MINUTES')
        disableConcurrentBuilds()
    }
    stages {
        stage('Build site') {
            agent { docker 'node:22' }
            steps {
                sh 'npm ci'
                sh 'npm run build'
            }
        }
        stage('Build image') {
            steps {
                dockerBuild(image: 'site')
            }
        }
        stage('Deploy') {
            steps {
                deploy(service: 'site', image: 'site')
                healthCheck(url: 'https://example.com', timeout: 60)
            }
        }
    }
    post {
        failure {
            echo 'The pipeline failed. Open the failed step above to read its log.'
        }
    }
}
