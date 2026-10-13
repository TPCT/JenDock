// Laravel app: build the image, test inside it, migrate on production, deploy, check health.
pipeline {
    agent none
    environment {
        APP_ENV = 'testing'
        DB_PASSWORD = credentials('DB_PASSWORD')
    }
    options {
        timeout(time: 30, unit: 'MINUTES')
        disableConcurrentBuilds()
    }
    stages {
        stage('Build image') {
            steps {
                dockerBuild(image: 'laravel-app', dockerfile: 'docker/Dockerfile', context: '.', target: 'production')
            }
        }
        stage('Test') {
            agent {
                docker {
                    image 'laravel-app'
                    args '--network none -e APP_ENV=testing --memory 1g --cpus 2'
                }
            }
            steps {
                sh 'composer install --no-interaction'
                sh 'php artisan test'
            }
        }
        stage('Migrate') {
            when { branch 'production' }
            agent { docker 'laravel-app' }
            steps {
                sh 'php artisan migrate --force'
            }
        }
        stage('Deploy') {
            steps {
                deploy service: 'app', image: 'laravel-app'
                healthCheck(url: 'https://example.test/up', expectStatus: 200, timeout: 90)
            }
        }
    }
    post {
        failure {
            echo "Build ${BUILD_NUMBER} failed on ${BRANCH_NAME}"
        }
    }
}
