pipeline {
    agent none
    stages {
        stage('Build') {
            steps {
                dockerBuild(image: 'etsawaa-app', dockerfile: 'Dockerfile', context: '.', target: 'app')
                dockerBuild(image: 'etsawaa-web', dockerfile: 'Dockerfile', context: '.', target: 'web')
                dockerBuild(image: 'etsawaa-frontend', dockerfile: 'docker/frontend.Dockerfile', context: '.', target: 'frontend')
                dockerBuild(image: 'deploy-test-etsawaa', dockerfile: 'Dockerfile', target: 'test')
            }
        }
        stage('Test') {
            agent {
                docker {
                    image 'deploy-test-etsawaa'
                }
            }
            environment {
                APP_ENV = 'testing'
                APP_KEY = 'base64:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA='
                CACHE_STORE = 'array'
                CACHE_DRIVER = 'array'
                SESSION_DRIVER = 'array'
                QUEUE_CONNECTION = 'sync'
                MAIL_MAILER = 'array'
                DB_CONNECTION = 'sqlite'
                DB_DATABASE = ':memory:'
            }
            steps {
                sh 'mkdir -p storage/framework/cache/data storage/framework/views storage/framework/sessions storage/logs && php vendor/bin/phpunit'
            }
        }
        stage('Deploy') {
            steps {
                deploy(services: [app: 'etsawaa-app', web: 'etsawaa-web', frontend: 'etsawaa-frontend', reverb: 'etsawaa-app', queue: 'etsawaa-app', scheduler: 'etsawaa-app'])
                healthCheck(url: 'https://etsawaa.com/', expectStatus: 200)
            }
        }
    }
}
