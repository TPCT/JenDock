pipeline {
    agent { docker { image 'node:22' } }
    stages {
        stage('Backend') {
            stages {
                stage('Build') { steps { sh 'make build' } }
                stage('Test') {
                    when { anyOf { branch 'main'; branch 'release/*' } }
                    steps { sh 'make test' }
                }
            }
        }
        stage('Services') {
            steps {
                dockerBuild(image: 'api.v2')
                dockerBuild(image: 'worker')
                deploy(services: ['api': 'api.v2', worker: 'worker'])
            }
        }
    }
}
