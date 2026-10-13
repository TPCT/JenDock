// Any project with a Dockerfile: build the image, deploy it and check that it answers.
// Before the first run, change two things: the service name in "deploy" (as named in your
// compose file) and the address in "healthCheck".
// To run tests first, add a stage between the two that uses the image, for example:
//   stage('Test') {
//       agent { docker 'app' }
//       steps { sh 'your test command' }
//   }
pipeline {
    agent none
    options {
        timeout(time: 30, unit: 'MINUTES')
        disableConcurrentBuilds()
    }
    stages {
        stage('Build image') {
            steps {
                dockerBuild(image: 'app')
            }
        }
        stage('Deploy') {
            steps {
                deploy(service: 'app', image: 'app')
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
