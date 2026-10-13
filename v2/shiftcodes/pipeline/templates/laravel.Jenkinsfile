// Laravel: build your Docker image, run the tests inside it, deploy it and check the site.
// Before the first run, change two things: the service name in "deploy" (as named in your
// compose file) and the address in "healthCheck".
// The image is built from the Dockerfile in your repository, so it must contain the PHP
// dependencies (including the ones for testing) for "php artisan test" to work.
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
        stage('Test') {
            agent { docker 'app' }
            steps {
                sh 'php artisan test'
            }
        }
        stage('Deploy') {
            steps {
                // To run database migrations before every deploy, add a stage that uses the
                // image and runs: sh 'php artisan migrate --force'
                deploy(service: 'app', image: 'app')
                healthCheck(url: 'https://example.com/up', timeout: 90)
            }
        }
    }
    post {
        failure {
            echo 'The pipeline failed. Open the failed step above to read its log.'
        }
    }
}
