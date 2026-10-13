pipeline {
    agent none
    stages {
        stage('Build') {
            steps {
                dockerBuild(image: 'dsl-captcha', dockerfile: 'Dockerfile', target: 'runtime')
                dockerBuild(image: 'deploy-test-captcha', dockerfile: 'Dockerfile', target: 'test')
            }
        }
        stage('Test') {
            agent {
                docker {
                    image 'deploy-test-captcha'
                }
            }
            steps {
                sh 'python tests/check_container.py'
            }
        }
        stage('Deploy') {
            steps {
                deploy(services: [captcha: 'dsl-captcha'])
                healthCheck(url: 'https://captcha.shiftcodes.net/health', expectStatus: 200)
            }
        }
    }
}
